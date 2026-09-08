import os

from datetime import datetime
from typing import Any

from protect_archiver.dataclasses import Camera
from protect_archiver.manifest import STATUS_OK
from protect_archiver.manifest import ArchiveManifest
from protect_archiver.reconcile import find_orphans
from protect_archiver.reconcile import parse_camera_label
from protect_archiver.reconcile import parse_segment_filename
from protect_archiver.utils import make_camera_name_fs_safe


CAMERA = Camera(id="abcd1234wxyz", name="Front Door", recording_start=datetime(2026, 1, 1))


def clip_name(start: datetime) -> str:
    label = make_camera_name_fs_safe(CAMERA)
    return f"{label} - {start.strftime('%Y-%m-%d - %H.%M.%S')}.mp4"


def write_clip(root: Any, start: datetime, content: bytes = b"y" * 2048) -> str:
    directory = root / start.strftime("%Y") / start.strftime("%m") / start.strftime("%d")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / clip_name(start)
    path.write_bytes(content)
    return str(path)


def test_parses_the_name_download_footage_writes() -> None:
    start = datetime(2026, 9, 7, 14, 0, 0)
    name = clip_name(start)

    assert parse_segment_filename(name) == start
    assert parse_camera_label(name) == "Front Door (wxyz)"


def test_parses_a_utc_suffixed_name() -> None:
    """--use-utc-filenames appends a numeric offset that must round-trip."""
    parsed = parse_segment_filename("Front Door (wxyz) - 2026-09-07 - 14.00.00+0000.mp4")

    assert parsed is not None
    assert parsed.utcoffset() is not None
    assert parsed.strftime("%Y-%m-%d %H:%M") == "2026-09-07 14:00"


def test_ignores_files_that_are_not_segments() -> None:
    assert parse_segment_filename("sync.state") is None
    assert parse_segment_filename("holiday video.mp4") is None


def test_adopts_existing_footage_so_it_is_not_downloaded_again(tmp_path: Any) -> None:
    start = datetime(2026, 9, 7, 14, 0, 0)
    path = write_clip(tmp_path, start)

    with ArchiveManifest(str(tmp_path)) as manifest:
        report = find_orphans(str(tmp_path), manifest, [CAMERA])

        assert report.adopted == 1
        assert not report.unmatched

        start_ms = int(start.timestamp() * 1e3)
        record = manifest.get(CAMERA.id, start_ms)
        assert record is not None
        assert record.status == STATUS_OK
        assert record.size == os.path.getsize(path)
        # Adoption deliberately records no hash; see the module docstring.
        assert record.sha256 == ""
        assert (CAMERA.id, start_ms) in manifest.settled_keys()


def test_adoption_is_idempotent(tmp_path: Any) -> None:
    write_clip(tmp_path, datetime(2026, 9, 7, 14, 0, 0))

    with ArchiveManifest(str(tmp_path)) as manifest:
        find_orphans(str(tmp_path), manifest, [CAMERA])
        second = find_orphans(str(tmp_path), manifest, [CAMERA])

        assert second.adopted == 0
        assert second.already_known == 1


def test_files_from_an_unknown_camera_are_reported_not_guessed(tmp_path: Any) -> None:
    write_clip(tmp_path, datetime(2026, 9, 7, 14, 0, 0))
    other = Camera(id="zzzz9999", name="Garage", recording_start=datetime(2026, 1, 1))

    with ArchiveManifest(str(tmp_path)) as manifest:
        report = find_orphans(str(tmp_path), manifest, [other])

        assert report.adopted == 0
        assert len(report.unmatched) == 1


def test_the_manifest_directory_is_not_walked(tmp_path: Any) -> None:
    write_clip(tmp_path, datetime(2026, 9, 7, 14, 0, 0))

    with ArchiveManifest(str(tmp_path)) as manifest:
        report = find_orphans(str(tmp_path), manifest, [CAMERA])

    assert report.adopted == 1
    assert not any(".protect-archive" in path for path in report.unmatched)
