import hashlib

from typing import Any

from protect_archiver.manifest import STATUS_EMPTY
from protect_archiver.manifest import STATUS_OK
from protect_archiver.manifest import SegmentRecord
from protect_archiver.verify import LEVEL_HASH
from protect_archiver.verify import LEVEL_NONE
from protect_archiver.verify import LEVEL_QUICK
from protect_archiver.verify import REASON_HASH_MISMATCH
from protect_archiver.verify import REASON_MISSING
from protect_archiver.verify import REASON_SIZE_MISMATCH
from protect_archiver.verify import verify_file
from protect_archiver.verify import verify_record


CONTENT = b"a" * 1024
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def write_clip(tmp_path: Any, name: str = "clip.mp4", content: bytes = CONTENT) -> str:
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def test_intact_file_passes_every_level(tmp_path: Any) -> None:
    path = write_clip(tmp_path)

    for level in (LEVEL_NONE, LEVEL_QUICK, LEVEL_HASH):
        assert verify_file(path, len(CONTENT), DIGEST, level).ok, level


def test_missing_file_is_caught_by_quick(tmp_path: Any) -> None:
    result = verify_file(str(tmp_path / "gone.mp4"), len(CONTENT), DIGEST, LEVEL_QUICK)

    assert not result.ok
    assert result.reason == REASON_MISSING


def test_truncated_file_is_caught_by_quick(tmp_path: Any) -> None:
    path = write_clip(tmp_path, content=CONTENT[:100])

    result = verify_file(path, len(CONTENT), DIGEST, LEVEL_QUICK)

    assert not result.ok
    assert result.reason == REASON_SIZE_MISMATCH


def test_silent_corruption_needs_hash_level(tmp_path: Any) -> None:
    """A file corrupted in place keeps its size, so only hashing finds it."""
    corrupted = bytearray(CONTENT)
    corrupted[500] = ord("b")
    path = write_clip(tmp_path, content=bytes(corrupted))

    assert verify_file(path, len(CONTENT), DIGEST, LEVEL_QUICK).ok

    result = verify_file(path, len(CONTENT), DIGEST, LEVEL_HASH)
    assert not result.ok
    assert result.reason == REASON_HASH_MISMATCH


def test_none_level_does_not_look_at_the_disk(tmp_path: Any) -> None:
    result = verify_file(str(tmp_path / "never-existed.mp4"), 10, "x", LEVEL_NONE)

    assert result.ok


def test_missing_hash_is_not_treated_as_corruption(tmp_path: Any) -> None:
    """Rows adopted from disk carry no hash; that must not fail verification."""
    path = write_clip(tmp_path)

    assert verify_file(path, len(CONTENT), "", LEVEL_HASH).ok


def test_empty_segments_need_no_file(tmp_path: Any) -> None:
    record = SegmentRecord(
        camera_id="cam1",
        camera_name="A",
        start_ms=0,
        end_ms=1,
        path="missing.mp4",
        size=0,
        sha256="",
        status=STATUS_EMPTY,
        downloaded_at="2026-01-01T00:00:00+00:00",
        verified_at=None,
    )

    assert verify_record(record, str(tmp_path / "missing.mp4"), LEVEL_HASH).ok


def test_ok_record_is_checked_against_disk(tmp_path: Any) -> None:
    record = SegmentRecord(
        camera_id="cam1",
        camera_name="A",
        start_ms=0,
        end_ms=1,
        path="missing.mp4",
        size=10,
        sha256="",
        status=STATUS_OK,
        downloaded_at="2026-01-01T00:00:00+00:00",
        verified_at=None,
    )

    result = verify_record(record, str(tmp_path / "missing.mp4"), LEVEL_QUICK)

    assert not result.ok
    assert result.reason == REASON_MISSING
