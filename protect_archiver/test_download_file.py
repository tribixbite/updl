import os
import time

from typing import Any

import pytest
import requests

from protect_archiver.client import ProtectClient
from protect_archiver.downloader.download_file import MINIMUM_CLIP_BYTES
from protect_archiver.downloader.download_file import download_file
from protect_archiver.errors import ProtectError
from protect_archiver.manifest import STATUS_EMPTY
from protect_archiver.manifest import STATUS_FAILED
from protect_archiver.manifest import STATUS_OK
from protect_archiver.utils import PART_SUFFIX


EXPORT_URL = "https://unifi:443/proxy/protect/api/video/export?camera=cam1&start=0&end=1"
QUERY = "/video/export?camera=cam1&start=0&end=1"
CLIP = b"x" * 4096


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: Any) -> None:
    """Retries back off exponentially; tests should not actually wait for it."""
    monkeypatch.setattr(time, "sleep", lambda _: None)


@pytest.fixture(autouse=True)
def mock_auth(responses: Any) -> None:
    responses.add(
        responses.POST,
        "https://unifi:443/api/auth/login",
        headers={"Set-Cookie": "TOKEN=token.token.token"},
        json={},
    )


@pytest.fixture
def client(test_output_dest: str) -> Any:
    return ProtectClient(
        destination_path=test_output_dest,
        password="test",
        ignore_failed_downloads=True,
    )


def target(test_output_dest: str, name: str = "clip.mp4") -> str:
    return os.path.join(test_output_dest, name)


def test_successful_download_writes_the_file_and_reports_its_hash(
    responses: Any, client: Any, test_output_dest: str
) -> None:
    responses.add(responses.GET, EXPORT_URL, body=CLIP, status=200)
    filename = target(test_output_dest)

    outcome = download_file(client, QUERY, filename)

    assert outcome.status == STATUS_OK
    assert outcome.size == len(CLIP)
    assert outcome.sha256
    assert open(filename, "rb").read() == CLIP
    assert client.files_downloaded == 1


def test_no_part_file_survives_a_successful_download(
    responses: Any, client: Any, test_output_dest: str
) -> None:
    responses.add(responses.GET, EXPORT_URL, body=CLIP, status=200)
    filename = target(test_output_dest)

    download_file(client, QUERY, filename)

    assert not os.path.exists(f"{filename}{PART_SUFFIX}")


def test_short_response_is_recorded_as_empty_not_written(
    responses: Any, client: Any, test_output_dest: str
) -> None:
    """The API answers 'no footage in this window' with a tiny body, not an error."""
    responses.add(responses.GET, EXPORT_URL, body=b"z" * (MINIMUM_CLIP_BYTES - 1), status=200)
    filename = target(test_output_dest)

    outcome = download_file(client, QUERY, filename)

    assert outcome.status == STATUS_EMPTY
    # Nothing unplayable is left behind in the archive.
    assert not os.path.exists(filename)
    assert not os.path.exists(f"{filename}{PART_SUFFIX}")


def test_transient_server_error_is_retried_then_succeeds(
    responses: Any, client: Any, test_output_dest: str
) -> None:
    """A 500 from the export endpoint used to become a permanent gap."""
    responses.add(responses.GET, EXPORT_URL, status=500, json={"error": "busy"})
    responses.add(responses.GET, EXPORT_URL, body=CLIP, status=200)
    filename = target(test_output_dest)

    outcome = download_file(client, QUERY, filename)

    assert outcome.status == STATUS_OK
    assert os.path.exists(filename)


def test_rate_limiting_is_retried(responses: Any, client: Any, test_output_dest: str) -> None:
    responses.add(responses.GET, EXPORT_URL, status=429, json={"error": "slow down"})
    responses.add(responses.GET, EXPORT_URL, body=CLIP, status=200)

    outcome = download_file(client, QUERY, target(test_output_dest))

    assert outcome.status == STATUS_OK


def test_permanent_client_error_is_not_retried(
    responses: Any, client: Any, test_output_dest: str
) -> None:
    """Repeating a request the server called malformed only wastes time."""
    responses.add(responses.GET, EXPORT_URL, status=404, json={"error": "no such camera"})

    outcome = download_file(client, QUERY, target(test_output_dest))

    assert outcome.status == STATUS_FAILED
    export_calls = [call for call in responses.calls if "video/export" in call.request.url]
    assert len(export_calls) == 1


def test_exhausted_retries_report_failure(
    responses: Any, client: Any, test_output_dest: str
) -> None:
    for _ in range(client.max_retries):
        responses.add(responses.GET, EXPORT_URL, status=503, json={"error": "unavailable"})

    outcome = download_file(client, QUERY, target(test_output_dest))

    assert outcome.status == STATUS_FAILED
    assert client.files_failed == 1


def test_failure_raises_unless_ignore_failed_downloads(
    responses: Any, test_output_dest: str
) -> None:
    strict_client = ProtectClient(
        destination_path=test_output_dest, password="test", ignore_failed_downloads=False
    )
    responses.add(responses.GET, EXPORT_URL, status=404, json={"error": "gone"})

    with pytest.raises(ProtectError):
        download_file(strict_client, QUERY, target(test_output_dest))


def test_interrupted_transfer_leaves_no_usable_file(
    responses: Any, client: Any, test_output_dest: str, monkeypatch: Any
) -> None:
    """The defect this replaces: a truncated write that later runs would skip forever."""
    responses.add(responses.GET, EXPORT_URL, body=CLIP, status=200)
    filename = target(test_output_dest)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise requests.exceptions.ConnectionError("connection reset mid-transfer")

    monkeypatch.setattr("requests.models.Response.iter_content", explode)

    outcome = download_file(client, QUERY, filename)

    assert outcome.status == STATUS_FAILED
    assert not os.path.exists(filename)
    assert not os.path.exists(f"{filename}{PART_SUFFIX}")


def test_expired_session_is_refreshed_without_spending_a_retry(
    responses: Any, client: Any, test_output_dest: str
) -> None:
    responses.add(responses.GET, EXPORT_URL, status=401, json={"error": "unauthorized"})
    responses.add(responses.GET, EXPORT_URL, body=CLIP, status=200)

    outcome = download_file(client, QUERY, target(test_output_dest))

    assert outcome.status == STATUS_OK
    # Two logins: the initial one, and the forced refresh after the 401.
    login_calls = [call for call in responses.calls if "auth/login" in call.request.url]
    assert len(login_calls) == 2


def test_skip_existing_files_short_circuits(responses: Any, test_output_dest: str) -> None:
    skipping_client = ProtectClient(
        destination_path=test_output_dest, password="test", skip_existing_files=True
    )
    filename = target(test_output_dest)
    with open(filename, "wb") as fp:
        fp.write(CLIP)

    outcome = download_file(skipping_client, QUERY, filename)

    assert outcome.status == STATUS_OK
    assert skipping_client.files_skipped == 1
    assert not responses.calls
