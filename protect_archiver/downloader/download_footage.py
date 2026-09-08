import logging
import time

from datetime import datetime
from datetime import timezone
from os import path
from typing import Any
from typing import Optional
from typing import Set

from protect_archiver.dataclasses import Camera
from protect_archiver.downloader.download_file import download_file
from protect_archiver.manifest import STATUS_EMPTY
from protect_archiver.manifest import STATUS_OK
from protect_archiver.manifest import ArchiveManifest
from protect_archiver.manifest import SegmentKey
from protect_archiver.utils import apply_recording_timestamp
from protect_archiver.utils import build_download_dir
from protect_archiver.utils import calculate_intervals
from protect_archiver.utils import make_camera_name_fs_safe
from protect_archiver.verify import LEVEL_QUICK
from protect_archiver.verify import verify_record


def _already_archived(
    manifest: ArchiveManifest,
    settled: Set[SegmentKey],
    key: SegmentKey,
    verify_level: str,
    require_audio: bool = False,
) -> bool:
    """Decide whether a segment can be skipped without contacting the NVR.

    The in-memory ``settled`` set answers the common case without a query. Only when it
    says the segment is already held do we read the row back, because that is the only
    branch that needs the recorded size and hash to check the file against.
    """
    if key not in settled:
        return False

    record = manifest.get(*key)
    if record is None:
        # The set and the table disagree, which should not happen; re-fetching is the
        # safe reading of an inconsistent index.
        return False

    result = verify_record(record, manifest.absolute_path(record), verify_level, require_audio)
    if result.ok:
        return True

    logging.warning(f"Re-downloading {record.path}: failed {verify_level} verification ({result})")
    return False


def download_footage(
    client: Any,
    start: datetime,
    end: datetime,
    camera: Camera,
    disable_alignment: bool = False,
    disable_splitting: bool = False,
    manifest: Optional[ArchiveManifest] = None,
    settled: Optional[Set[SegmentKey]] = None,
    verify_level: str = LEVEL_QUICK,
    require_audio: bool = False,
) -> None:
    # make camera name safe for use in file name
    camera_name_fs_safe = make_camera_name_fs_safe(camera)
    settled_keys = settled if settled is not None else set()

    logging.info(f"Downloading footage for camera '{camera.name}' ({camera.id})")

    # split requested time frame into chunks of 1 hour or less and download them one by one
    for interval_start, interval_end in calculate_intervals(
        start,
        end,
        disable_alignment,
        disable_splitting,
    ):
        # start and end time of the video segment to be downloaded
        js_timestamp_range_start = int(interval_start.timestamp() * 1e3)
        js_timestamp_range_end = int(interval_end.timestamp() * 1e3)

        # A segment is identified by its camera and time range, never by its path, so
        # that renaming a camera or changing the folder layout cannot make the archive
        # look empty. This check happens before any directory is created, so skipped
        # segments leave no trace on disk.
        segment_key: SegmentKey = (camera.id, js_timestamp_range_start)
        if manifest is not None and _already_archived(
            manifest, settled_keys, segment_key, verify_level, require_audio
        ):
            logging.debug(
                f"Segment {interval_start} - {interval_end} for '{camera.name}' is already"
                " archived - skipping"
            )
            client.files_already_archived += 1
            continue

        # wait n seconds before starting next download (if parameter is set)
        if client.download_wait != 0 and client.files_downloaded == 0:
            logging.debug(
                "Command line argument '--wait-between-downloads' is set to"
                f" {client.download_wait} second(s)... \n"
            )
            time.sleep(int(client.download_wait))

        # support selection between local time zone and UTC for file names
        interval_start_tz = (
            interval_start.astimezone(timezone.utc) if client.use_utc_filenames else interval_start
        )

        download_dir = build_download_dir(
            use_subfolders=client.use_subfolders,
            destination_path=client.destination_path,
            interval_start_tz=interval_start_tz,
            camera_name_fs_safe=camera_name_fs_safe,
        )

        # file name for download
        filename_timestamp = interval_start_tz.strftime("%Y-%m-%d - %H.%M.%S%z")
        filename = f"{download_dir}/{camera_name_fs_safe} - {filename_timestamp}.mp4"

        logging.info(
            f"Downloading video for time range {interval_start} - {interval_end} to {filename}"
        )

        # create file without content if argument --touch-files is present
        # XXX(dcramer): would be nice to document why you'd ever want this
        if bool(client.touch_files) and not path.exists(filename):
            logging.debug(f"Argument '--touch-files' is present. Creating file at {filename}")
            open(filename, "a").close()

        # build video export query
        video_export_query = f"/video/export?camera={camera.id}&start={js_timestamp_range_start}&end={js_timestamp_range_end}"

        # download the file
        outcome = download_file(client, video_export_query, filename)

        if outcome.status == STATUS_OK:
            # Stamp the file with when the footage happened rather than when it was
            # fetched, so the archive sorts by event time on disk.
            apply_recording_timestamp(filename, interval_start)

        if manifest is not None:
            manifest.record(
                camera_id=camera.id,
                camera_name=camera.name,
                start_ms=js_timestamp_range_start,
                end_ms=js_timestamp_range_end,
                # An empty segment has no file, so record the path it would have taken
                # to keep the row self-describing.
                filename=filename,
                size=outcome.size,
                sha256=outcome.sha256,
                status=outcome.status,
            )
            if outcome.succeeded:
                settled_keys.add(segment_key)
            if outcome.status == STATUS_EMPTY:
                logging.debug(
                    f"Recorded {interval_start} - {interval_end} for '{camera.name}' as empty"
                )
