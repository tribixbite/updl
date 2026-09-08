"""End-to-end behaviour of a sync run, which is where 'do not download it twice' lives."""

import os
import time

from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest

from protect_archiver.client import ProtectClient
from protect_archiver.dataclasses import Camera
from protect_archiver.manifest import STATUS_EMPTY
from protect_archiver.manifest import STATUS_FAILED
from protect_archiver.manifest import STATUS_OK
from protect_archiver.manifest import ArchiveManifest
from protect_archiver.sync import ProtectSync
from protect_archiver.utils import PART_SUFFIX
from protect_archiver.utils import apply_recording_timestamp
from protect_archiver.verify import LEVEL_HASH
from protect_archiver.verify import LEVEL_QUICK
from protect_archiver.verify import sha256_file


CLIP = b"v" * 4096


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: Any) -> None:
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
def camera() -> Camera:
    # Three whole hours ending on the last complete hour, so the sweep is small and
    # every interval is hour-aligned.
    top_of_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    return Camera(id="cam1", name="Front Door", recording_start=top_of_hour - timedelta(hours=3))


@pytest.fixture
def client(test_output_dest: str) -> Any:
    return ProtectClient(
        destination_path=test_output_dest,
        password="test",
        use_subfolders=True,
        ignore_failed_downloads=True,
    )


def export_calls(responses: Any) -> list:
    return [call for call in responses.calls if "video/export" in call.request.url]


def run_sync(client: Any, camera: Camera, dest: str, **kwargs: Any) -> None:
    ProtectSync(client=client, destination_path=dest, statefile="sync.state").run(
        [camera], **kwargs
    )


def test_first_run_downloads_every_hour(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )

    run_sync(client, camera, test_output_dest)

    assert len(export_calls(responses)) == 3
    assert client.files_downloaded == 3

    with ArchiveManifest(test_output_dest) as manifest:
        assert manifest.status_counts() == {STATUS_OK: 3}


def test_second_run_downloads_nothing(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    """The whole point: running again later must not refetch what is already held."""
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )
    run_sync(client, camera, test_output_dest)
    first_run_calls = len(export_calls(responses))

    second_client = ProtectClient(
        destination_path=test_output_dest, password="test", use_subfolders=True
    )
    run_sync(second_client, camera, test_output_dest)

    assert len(export_calls(responses)) == first_run_calls
    assert second_client.files_downloaded == 0
    assert second_client.files_already_archived == 3


def test_a_deleted_file_is_fetched_again(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )
    run_sync(client, camera, test_output_dest)

    with ArchiveManifest(test_output_dest) as manifest:
        record = next(manifest.iter_segments())
        os.remove(manifest.absolute_path(record))

    second_client = ProtectClient(
        destination_path=test_output_dest, password="test", use_subfolders=True
    )
    run_sync(second_client, camera, test_output_dest)

    assert second_client.files_downloaded == 1
    assert second_client.files_already_archived == 2


def test_a_truncated_file_is_fetched_again(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )
    run_sync(client, camera, test_output_dest)

    with ArchiveManifest(test_output_dest) as manifest:
        record = next(manifest.iter_segments())
        with open(manifest.absolute_path(record), "wb") as fp:
            fp.write(CLIP[:100])

    second_client = ProtectClient(
        destination_path=test_output_dest, password="test", use_subfolders=True
    )
    run_sync(second_client, camera, test_output_dest)

    assert second_client.files_downloaded == 1


def test_silent_corruption_is_only_caught_at_hash_level(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )
    run_sync(client, camera, test_output_dest)

    with ArchiveManifest(test_output_dest) as manifest:
        record = next(manifest.iter_segments())
        corrupted = bytearray(CLIP)
        corrupted[10] = ord("Z")
        with open(manifest.absolute_path(record), "wb") as fp:
            fp.write(bytes(corrupted))

    # Same length, so a quick check is satisfied.
    quick_client = ProtectClient(
        destination_path=test_output_dest, password="test", use_subfolders=True
    )
    run_sync(quick_client, camera, test_output_dest, verify_level=LEVEL_QUICK)
    assert quick_client.files_downloaded == 0

    hash_client = ProtectClient(
        destination_path=test_output_dest, password="test", use_subfolders=True
    )
    run_sync(hash_client, camera, test_output_dest, verify_level=LEVEL_HASH)
    assert hash_client.files_downloaded == 1


def test_an_hour_with_no_footage_is_recorded_and_never_requested_again(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=b"tiny", status=200
    )

    run_sync(client, camera, test_output_dest)
    first_run_calls = len(export_calls(responses))

    with ArchiveManifest(test_output_dest) as manifest:
        assert manifest.status_counts() == {STATUS_EMPTY: 3}

    second_client = ProtectClient(
        destination_path=test_output_dest, password="test", use_subfolders=True
    )
    run_sync(second_client, camera, test_output_dest)

    assert len(export_calls(responses)) == first_run_calls


def test_a_failed_hour_is_retried_by_the_next_run(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    """The gap the upstream statefile cursor would have skipped past permanently."""
    responses.add(
        responses.GET,
        "https://unifi:443/proxy/protect/api/video/export",
        status=404,
        json={"error": "nope"},
    )

    run_sync(client, camera, test_output_dest)

    with ArchiveManifest(test_output_dest) as manifest:
        assert manifest.status_counts() == {STATUS_FAILED: 3}

    responses.reset()
    responses.add(
        responses.POST,
        "https://unifi:443/api/auth/login",
        headers={"Set-Cookie": "TOKEN=token.token.token"},
        json={},
    )
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )

    second_client = ProtectClient(
        destination_path=test_output_dest,
        password="test",
        use_subfolders=True,
        ignore_failed_downloads=True,
    )
    run_sync(second_client, camera, test_output_dest)

    assert second_client.files_downloaded == 3
    with ArchiveManifest(test_output_dest) as manifest:
        assert manifest.status_counts() == {STATUS_OK: 3}


def test_recording_gaps_are_settled_not_retried_forever(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    """Hours the camera was offline must settle as empty, or every run re-requests them."""
    responses.add(
        responses.GET,
        "https://unifi:443/proxy/protect/api/video/export",
        status=404,
        json={"error": 502, "operationId": 1},
    )

    run_sync(client, camera, test_output_dest)
    first_run_calls = len(export_calls(responses))

    with ArchiveManifest(test_output_dest) as manifest:
        assert manifest.status_counts() == {STATUS_EMPTY: 3}

    second_client = ProtectClient(
        destination_path=test_output_dest, password="test", use_subfolders=True
    )
    run_sync(second_client, camera, test_output_dest)

    assert len(export_calls(responses)) == first_run_calls
    assert second_client.files_downloaded == 0


def test_ignore_state_refetches_everything(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )
    run_sync(client, camera, test_output_dest)

    second_client = ProtectClient(
        destination_path=test_output_dest, password="test", use_subfolders=True
    )
    run_sync(second_client, camera, test_output_dest, ignore_state=True)

    assert second_client.files_downloaded == 3


def test_stale_part_files_are_swept_at_startup(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )
    debris = os.path.join(test_output_dest, f"leftover.mp4{PART_SUFFIX}")
    with open(debris, "wb") as fp:
        fp.write(b"half a clip")

    run_sync(client, camera, test_output_dest)

    assert not os.path.exists(debris)


def test_a_camera_with_no_recordings_is_skipped_not_swept_from_year_one(
    responses: Any, client: Any, test_output_dest: str
) -> None:
    """datetime.min as a start would otherwise expand to millions of requests."""
    offline = Camera(id="cam2", name="Offline", recording_start=datetime.min)

    run_sync(client, offline, test_output_dest)

    assert not export_calls(responses)
    assert client.files_downloaded == 0


def test_downloaded_files_carry_the_recording_time_not_the_download_time(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    """The export endpoint dates its MP4s at the moment of export.

    Left alone, an archive fetched today shows today's date for last week's footage, so
    anything sorting by file date sees when it was collected, not when it happened.
    """
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )

    run_sync(client, camera, test_output_dest)

    with ArchiveManifest(test_output_dest) as manifest:
        for record in manifest.iter_segments():
            expected = record.start_ms / 1000
            actual = os.path.getmtime(manifest.absolute_path(record))
            assert abs(actual - expected) < 2, record.path


def test_fixing_timestamps_leaves_the_bytes_and_hash_alone(
    responses: Any, client: Any, camera: Camera, test_output_dest: str
) -> None:
    responses.add(
        responses.GET, "https://unifi:443/proxy/protect/api/video/export", body=CLIP, status=200
    )
    run_sync(client, camera, test_output_dest)

    with ArchiveManifest(test_output_dest) as manifest:
        record = next(manifest.iter_segments())
        path = manifest.absolute_path(record)
        digest_before = sha256_file(path)
        os.utime(path, (0, 0))

        apply_recording_timestamp(path, datetime.fromtimestamp(record.start_ms / 1000))

        assert sha256_file(path) == digest_before
        assert abs(os.path.getmtime(path) - record.start_ms / 1000) < 2
