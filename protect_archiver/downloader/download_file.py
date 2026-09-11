# file downloader
import hashlib
import json
import logging
import os
import time

from dataclasses import dataclass
from typing import Any
from typing import Dict

import requests

from protect_archiver.errors import ProtectError
from protect_archiver.manifest import STATUS_EMPTY
from protect_archiver.manifest import STATUS_FAILED
from protect_archiver.manifest import STATUS_OK
from protect_archiver.utils import PART_SUFFIX
from protect_archiver.utils import format_bytes
from protect_archiver.utils import print_download_stats

# A response shorter than this is the Protect API's way of saying "there is no footage
# in that window" rather than a real clip.
MINIMUM_CLIP_BYTES = 300

# Statuses worth trying again: the export endpoint routinely 500s on a busy NVR, and
# answers 429 when asked for too much at once. A 4xx other than these means the request
# itself was wrong, and repeating it verbatim will not help.
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})

# "There is no footage in that range" is reported as an HTTP 404 whose body is
# {"error": 502} -- a confusing pair, since neither number means what it appears to.
# Verified against a UDM Pro SE on 2026-09-07 by requesting a range 30 days in the future
# and one long before the NVR's retention began: both answer with exactly this, while the
# hours either side of a real gap return 200 and video.
#
# Distinguishing it matters. Treated as a failure, every hour a camera happened to be
# offline would be re-requested on every future run, for as long as the archive exists,
# and each run would end by reporting failures that no amount of retrying can fix.
NO_FOOTAGE_STATUS = 404
NO_FOOTAGE_ERROR_CODE = 502

# Cap the exponential backoff so a long overnight run cannot end up sleeping for hours
# between attempts.
MAXIMUM_RETRY_DELAY_SECONDS = 300

# Read in 1 MiB blocks so the hash is computed as the bytes stream past, rather than by
# reading the finished file back off disk.
STREAM_BLOCK_SIZE = 1024 * 1024


@dataclass
class DownloadOutcome:
    """What happened to one requested segment.

    Returned rather than recorded here so that the caller, which knows the segment's
    camera and time range, owns the manifest write. This keeps the transfer logic
    unaware of the archive index.
    """

    status: str
    size: int = 0
    sha256: str = ""
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status in (STATUS_OK, STATUS_EMPTY)


def _authenticated_get(client: Any, uri: str, force_token_refresh: bool = False) -> Any:
    """Issue the export request with whichever auth scheme this session uses."""
    token = client.session.get_api_token(force=force_token_refresh)
    common: Dict[str, Any] = {
        "verify": client.verify_ssl,
        "timeout": client.download_timeout,
        "stream": True,
    }
    if client.session.__class__.__name__ == "UniFiOSClient":
        return requests.get(
            uri,
            cookies={"TOKEN": token, "UOS_TOKEN": token},
            headers={"Authorization": f"Bearer {token}"},
            **common,
        )
    return requests.get(uri, headers={"Authorization": f"Bearer {token}"}, **common)


def _response_body(response: Any) -> Any:
    """Parse a response body as JSON, or return None if it is not."""
    try:
        return json.loads(response.content)
    except Exception:
        return None


def _error_message(response: Any) -> str:
    data = _response_body(response)
    if data is None:
        return "(no information available)"
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or data)
    return str(data)


def _reports_no_footage(response: Any) -> bool:
    """Whether the NVR is saying this range holds no footage, rather than erroring.

    Deliberately narrow: a bare 404 still counts as a failure, because it is also what a
    genuinely malformed request produces. Only the exact pairing documented above is
    read as an empty range.
    """
    if response.status_code != NO_FOOTAGE_STATUS:
        return False
    data = _response_body(response)
    return isinstance(data, dict) and data.get("error") == NO_FOOTAGE_ERROR_CODE


def _stream_to_file(response: Any, filename: str) -> DownloadOutcome:
    """Write a response body to disk atomically, hashing it on the way through.

    The bytes land in a sibling ``.part`` file which is renamed into place only once the
    transfer completes. Without this, killing a run mid-write leaves a truncated MP4
    that is indistinguishable from a complete one, and every later run skips it.
    """
    part_filename = f"{filename}{PART_SUFFIX}"
    digest = hashlib.sha256()
    written = 0

    os.makedirs(os.path.dirname(part_filename) or ".", exist_ok=True)

    try:
        with open(part_filename, "wb") as fp:
            for chunk in response.iter_content(STREAM_BLOCK_SIZE):
                if not chunk:
                    continue
                written += len(chunk)
                digest.update(chunk)
                fp.write(chunk)
    except Exception:
        if os.path.exists(part_filename):
            os.remove(part_filename)
        raise

    if written < MINIMUM_CLIP_BYTES:
        # No footage for this window. Remove the stub rather than leaving an unplayable
        # file in the archive, and report it so it is recorded and never re-requested.
        os.remove(part_filename)
        return DownloadOutcome(
            status=STATUS_EMPTY, detail=f"{written} bytes, below the {MINIMUM_CLIP_BYTES} minimum"
        )

    os.replace(part_filename, filename)
    return DownloadOutcome(status=STATUS_OK, size=written, sha256=digest.hexdigest())


def _handle_failure(client: Any, outcome: DownloadOutcome, exit_code: int) -> DownloadOutcome:
    """Apply the caller's chosen policy for a segment that could not be downloaded."""
    client.files_failed += 1

    if not client.ignore_failed_downloads:
        logging.info(
            "To skip failed downloads and continue with next file, add argument"
            " '--ignore-failed-downloads'"
        )
        print_download_stats(client)
        raise ProtectError(exit_code)

    logging.info("Argument '--ignore-failed-downloads' is present, continue downloading files...")
    return outcome


def download_file(client: Any, query: str, filename: str) -> DownloadOutcome:
    """Download one segment, retrying transient failures, and report what happened."""
    exit_code = 1
    base_delay = max(client.download_wait, 3)
    uri = f"{client.session.authority}{client.session.base_path}{query}"
    last_detail = ""

    # skip downloading files that already exist on disk if argument --skip-existing-files is present
    if bool(client.skip_existing_files) and os.path.exists(filename):
        logging.info(
            f"File {filename} already exists on disk and argument '--skip-existing-files' "
            "is present - skipping download \n"
        )
        client.files_skipped += 1
        return DownloadOutcome(
            status=STATUS_OK,
            size=os.path.getsize(filename),
            detail="already present on disk",
        )

    for retry_num in range(client.max_retries):
        try:
            start = time.monotonic()
            response = _authenticated_get(client, uri)

            if response.status_code == 401:
                # An expired session is not a transport failure and must not consume the
                # retry budget: refresh the token once and reissue immediately.
                response = _authenticated_get(client, uri, force_token_refresh=True)

            if response.status_code != 200:
                if _reports_no_footage(response):
                    logging.info(
                        "The NVR holds no footage for this time range - recording it as"
                        " empty so it is not requested again"
                    )
                    client.files_skipped += 1
                    return DownloadOutcome(
                        status=STATUS_EMPTY, detail="NVR reports no footage for this range"
                    )

                last_detail = (
                    f"{response.status_code} {response.reason}: {_error_message(response)}"
                )
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    logging.error(f"Download failed, not retryable: {last_detail}")
                    return _handle_failure(
                        client, DownloadOutcome(status=STATUS_FAILED, detail=last_detail), 4
                    )

                logging.warning(f"Download failed: {last_detail}")
                exit_code = 4
            else:
                outcome = _stream_to_file(response, filename)

                if outcome.status == STATUS_EMPTY:
                    logging.info(
                        f"No footage available for this segment ({outcome.detail})"
                        " - recording it as empty"
                    )
                    client.files_skipped += 1
                    return outcome

                elapsed = max(time.monotonic() - start, 1e-6)
                logging.info(
                    f"Download successful after {int(elapsed)}s ({format_bytes(outcome.size)}, "
                    f"{format_bytes(int(outcome.size // elapsed))}ps)"
                )
                client.files_downloaded += 1
                client.bytes_downloaded += outcome.size
                return outcome

        except requests.exceptions.RequestException as request_exception:
            last_detail = str(request_exception)
            logging.warning(f"Download failed: {request_exception}")
            exit_code = 5

        except OSError as os_error:
            # A full or disconnected destination volume will not fix itself by retrying,
            # and continuing would silently produce an incomplete archive.
            logging.error(f"Could not write {filename}: {os_error}")
            return _handle_failure(
                client, DownloadOutcome(status=STATUS_FAILED, detail=str(os_error)), 5
            )

        if retry_num < client.max_retries - 1:
            delay = min(base_delay * (2**retry_num), MAXIMUM_RETRY_DELAY_SECONDS)
            logging.warning(
                f"Retrying in {delay} second(s) (attempt {retry_num + 2} of {client.max_retries})..."
            )
            time.sleep(delay)

    return _handle_failure(
        client, DownloadOutcome(status=STATUS_FAILED, detail=last_detail), exit_code
    )
