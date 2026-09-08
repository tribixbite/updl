"""Integrity checks for files the archive believes it already holds.

Skipping a download because a file exists is only safe if "exists" implies "is intact".
It does not: an interrupted transfer leaves a truncated MP4 that looks like a finished
one, and a failing disk can rot a file that was written correctly years ago. So the
archiver records what it wrote and offers three increasingly thorough ways to confirm
that what is on disk still matches:

``none``
    trust the manifest and never touch the disk. Fastest, and appropriate only when the
    archive is known good and very large.
``quick`` (the default)
    the file exists and its size matches the manifest. One ``stat`` per segment, cheap
    enough to run over the whole archive on every sync, and it catches the common
    failure -- a truncated or deleted file.
``hash``
    the file's SHA-256 still matches. Reads every byte, so it catches silent corruption,
    but costs a full pass over the archive.
``deep``
    additionally asks ffprobe to decode the container and report a sane duration. This
    is the only level that catches a file which is the right length and hashes to what
    was written, but was written from a truncated response the NVR served as a 200.

Anything that fails at the requested level is re-downloaded.
"""

import hashlib
import logging
import os
import shutil
import subprocess

from dataclasses import dataclass
from typing import Optional

from protect_archiver.manifest import STATUS_EMPTY
from protect_archiver.manifest import SegmentRecord


LEVEL_NONE = "none"
LEVEL_QUICK = "quick"
LEVEL_HASH = "hash"
LEVEL_DEEP = "deep"

# Ordered cheapest first; the CLI presents them in this order too.
VERIFY_LEVELS = (LEVEL_NONE, LEVEL_QUICK, LEVEL_HASH, LEVEL_DEEP)

# Reasons a segment can fail verification, used for both logs and the audit summary.
REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_SIZE_MISMATCH = "size-mismatch"
REASON_HASH_MISMATCH = "hash-mismatch"
REASON_UNDECODABLE = "undecodable"

# Read in 1 MiB blocks: large enough that syscall overhead disappears against disk
# throughput, small enough not to matter for memory on any machine running this.
HASH_BLOCK_SIZE = 1024 * 1024

# How long to let ffprobe look at a single file before giving up on it.
FFPROBE_TIMEOUT_SECONDS = 30

# Remembered so a missing ffprobe is reported once per run rather than per file.
_ffprobe_warning_issued = False


@dataclass
class VerifyResult:
    """The outcome of checking one segment against the manifest."""

    ok: bool
    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return self.reason if not self.detail else f"{self.reason} ({self.detail})"


def sha256_file(path: str) -> str:
    """Return the hex SHA-256 of a file, reading it in blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(HASH_BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe_duration_seconds(path: str) -> Optional[float]:
    """Return the container duration ffprobe reports, or None if it cannot read it."""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logging.debug(f"ffprobe could not run against {path}: {error}")
        return None

    if completed.returncode != 0:
        return None

    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def _deep_check(path: str) -> VerifyResult:
    """Confirm the container actually decodes, degrading gracefully without ffprobe."""
    global _ffprobe_warning_issued

    if not ffprobe_available():
        if not _ffprobe_warning_issued:
            logging.warning(
                "ffprobe was not found on PATH, so --verify=deep cannot inspect video"
                " containers; falling back to hash verification for this run."
            )
            _ffprobe_warning_issued = True
        return VerifyResult(ok=True, reason=REASON_OK)

    duration = probe_duration_seconds(path)
    if duration is None:
        return VerifyResult(ok=False, reason=REASON_UNDECODABLE, detail="ffprobe could not read it")
    if duration <= 0:
        return VerifyResult(ok=False, reason=REASON_UNDECODABLE, detail="zero duration")
    return VerifyResult(ok=True, reason=REASON_OK)


def verify_file(path: str, expected_size: int, expected_sha256: str, level: str) -> VerifyResult:
    """Check one file on disk against what the manifest says it should be."""
    if level == LEVEL_NONE:
        # Explicitly asked not to look at the disk at all.
        return VerifyResult(ok=True, reason=REASON_OK, detail="not checked")

    if not os.path.isfile(path):
        return VerifyResult(ok=False, reason=REASON_MISSING)

    actual_size = os.path.getsize(path)
    if actual_size != expected_size:
        return VerifyResult(
            ok=False,
            reason=REASON_SIZE_MISMATCH,
            detail=f"expected {expected_size} bytes, found {actual_size}",
        )

    if level == LEVEL_QUICK:
        return VerifyResult(ok=True, reason=REASON_OK)

    # An older manifest row, or one adopted by --reconcile, may carry no hash. Treat
    # that as "nothing to compare against" rather than as a failure.
    if expected_sha256:
        try:
            actual_sha256 = sha256_file(path)
        except OSError as error:
            return VerifyResult(ok=False, reason=REASON_MISSING, detail=str(error))
        if actual_sha256 != expected_sha256:
            return VerifyResult(
                ok=False,
                reason=REASON_HASH_MISMATCH,
                detail=f"expected {expected_sha256[:12]}..., found {actual_sha256[:12]}...",
            )

    if level == LEVEL_DEEP:
        return _deep_check(path)

    return VerifyResult(ok=True, reason=REASON_OK)


def verify_record(record: SegmentRecord, absolute_path: str, level: str) -> VerifyResult:
    """Check one manifest row, accounting for rows that intentionally have no file."""
    if record.status == STATUS_EMPTY:
        # The NVR reported no footage for this hour, so there is no file to check and
        # nothing to re-download.
        return VerifyResult(ok=True, reason=REASON_OK, detail="no footage recorded")

    return verify_file(absolute_path, record.size, record.sha256, level)
