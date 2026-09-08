import os

from typing import Any

from protect_archiver.manifest import STATUS_EMPTY
from protect_archiver.manifest import STATUS_FAILED
from protect_archiver.manifest import STATUS_OK
from protect_archiver.manifest import ArchiveManifest
from protect_archiver.manifest import to_archive_relative


def make_manifest(tmp_path: Any) -> ArchiveManifest:
    return ArchiveManifest(str(tmp_path))


def test_records_and_reads_back_a_segment(tmp_path: Any) -> None:
    with make_manifest(tmp_path) as manifest:
        manifest.record(
            camera_id="cam1",
            camera_name="Front Door",
            start_ms=1000,
            end_ms=4599999,
            filename=str(tmp_path / "2026" / "01" / "clip.mp4"),
            size=1234,
            sha256="abc123",
            status=STATUS_OK,
        )

        record = manifest.get("cam1", 1000)
        assert record is not None
        assert record.camera_name == "Front Door"
        assert record.size == 1234
        assert record.status == STATUS_OK
        # Paths are stored relative to the archive so it survives being moved.
        assert record.path == "2026/01/clip.mp4"
        assert not os.path.isabs(record.path)


def test_settled_keys_covers_ok_and_empty_but_not_failed(tmp_path: Any) -> None:
    with make_manifest(tmp_path) as manifest:
        manifest.record("cam1", "A", 1000, 2000, str(tmp_path / "a.mp4"), 10, "h", STATUS_OK)
        manifest.record("cam1", "A", 3000, 4000, str(tmp_path / "b.mp4"), 0, "", STATUS_EMPTY)
        manifest.record("cam1", "A", 5000, 6000, str(tmp_path / "c.mp4"), 0, "", STATUS_FAILED)

        settled = manifest.settled_keys()

        assert ("cam1", 1000) in settled
        # An hour the NVR had no footage for must never be requested again.
        assert ("cam1", 3000) in settled
        # A failed hour must stay eligible, so a later run fills the gap.
        assert ("cam1", 5000) not in settled


def test_recording_the_same_segment_again_updates_it(tmp_path: Any) -> None:
    with make_manifest(tmp_path) as manifest:
        manifest.record("cam1", "A", 1000, 2000, str(tmp_path / "a.mp4"), 0, "", STATUS_FAILED)
        manifest.record("cam1", "A", 1000, 2000, str(tmp_path / "a.mp4"), 500, "hash", STATUS_OK)

        assert manifest.status_counts() == {STATUS_OK: 1}
        record = manifest.get("cam1", 1000)
        assert record is not None
        assert record.sha256 == "hash"


def test_mark_for_redownload_demotes_a_segment(tmp_path: Any) -> None:
    with make_manifest(tmp_path) as manifest:
        manifest.record("cam1", "A", 1000, 2000, str(tmp_path / "a.mp4"), 10, "h", STATUS_OK)
        record = manifest.get("cam1", 1000)
        assert record is not None

        manifest.mark_for_redownload(record)

        assert ("cam1", 1000) not in manifest.settled_keys()
        assert manifest.status_counts() == {STATUS_FAILED: 1}


def test_layout_change_is_reported(tmp_path: Any) -> None:
    with make_manifest(tmp_path) as manifest:
        assert manifest.check_layout(use_subfolders=True, use_utc_filenames=False) is None
        # Same options again: nothing to say.
        assert manifest.check_layout(use_subfolders=True, use_utc_filenames=False) is None

        warning = manifest.check_layout(use_subfolders=True, use_utc_filenames=True)

        assert warning is not None
        assert "use_utc_filenames" in warning


def test_manifest_persists_across_reopen(tmp_path: Any) -> None:
    with make_manifest(tmp_path) as manifest:
        manifest.record("cam1", "A", 1000, 2000, str(tmp_path / "a.mp4"), 10, "h", STATUS_OK)

    with make_manifest(tmp_path) as reopened:
        assert reopened.get("cam1", 1000) is not None
        assert reopened.settled_keys() == {("cam1", 1000)}


def test_to_archive_relative_normalises_separators(tmp_path: Any) -> None:
    nested = os.path.join(str(tmp_path), "2026", "09", "clip.mp4")
    assert to_archive_relative(str(tmp_path), nested) == "2026/09/clip.mp4"
