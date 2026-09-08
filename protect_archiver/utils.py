import logging
import os

from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Iterable
from typing import List
from typing import Tuple

from protect_archiver.dataclasses import Camera


# Suffix for a download still in flight. The bytes are written here and renamed into
# place only on success, so an interrupted run never leaves a truncated file that later
# runs would mistake for a complete one.
PART_SUFFIX = ".part"


def json_encode(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


# return time difference between given date_time_object and next full hour
def diff_round_up_to_full_hour(date_time_object: datetime) -> datetime:
    if date_time_object.minute != 0 or date_time_object.second != 0:
        return date_time_object.replace(
            second=0, microsecond=0, minute=0, hour=date_time_object.hour
        ) + timedelta(hours=1, minutes=0)
    else:
        return date_time_object.replace(
            second=0, microsecond=0, minute=0, hour=date_time_object.hour
        ) + timedelta(hours=0, minutes=0)


# return time difference between given date_time_object and past full hour
def diff_round_down_to_full_hour(date_time_object: datetime) -> datetime:
    return date_time_object.replace(
        second=0, microsecond=0, minute=0, hour=date_time_object.hour
    ) + timedelta(hours=0, minutes=0)


# calculate and yield the intervals between the given start and end datetime objects
# - Calculates intervals in 1-hour segments, aligning them with absolute hours.
#   Shorter segments may be present at the start and/or end.
# - Supports disabling alignment to absolute hours.
# - Supports disabling the splitting into 1-hour segments (use with caution).
def calculate_intervals(
    start: datetime,
    end: datetime,
    disable_alignment: bool = False,
    disable_splitting: bool = False,
) -> Iterable[Tuple[datetime, datetime]]:
    # if true, do not split into 1-hour segments
    # Caution: this can cause the Protect application to crash and restart unexpectedly!
    if disable_splitting:
        yield start, end - timedelta(milliseconds=1)  # yield everything at once
        return  # exit early

    # if true, disable alignment to absolute hours
    if disable_alignment:
        # divide total duration by 60 min, yield 1-hour segments
        for _ in range(int((end - start).seconds / 3600)):
            yield start, start + timedelta(minutes=59, seconds=59, milliseconds=999)
            start = start + timedelta(hours=1)
        yield start, end - timedelta(milliseconds=1)  # yield remaining segment
        return  # exit early

    #####
    # if none of the options above were used, calculate 1-hour segments and align them with absolute hours
    #####

    # calculate time differences to next or past full hour
    start_diff_to_next_full_hour = diff_round_up_to_full_hour(start) - start
    end_diff_to_past_full_hour = end - diff_round_down_to_full_hour(end)

    # save original start and end for later
    original_start = start
    original_end = end

    # yield interval from start to first full hour and align start to full hours
    # but only if the end datetime is past the next full hour after start datetime
    if (
        start_diff_to_next_full_hour.seconds != 0
        and (original_end - original_start) >= start_diff_to_next_full_hour
    ):
        yield start, start + (start_diff_to_next_full_hour - timedelta(milliseconds=1))
        start = start + start_diff_to_next_full_hour  # update start time

    # yield all remaining full-hour intervals
    for _ in range(int((end - start).total_seconds() / 3600)):
        yield start, start + timedelta(minutes=59, seconds=59, milliseconds=999)
        start = start + timedelta(minutes=60)  # update start time

    # if end is not on full hour, yield remaining segment
    if end_diff_to_past_full_hour.seconds != 0:
        yield start, original_end - timedelta(milliseconds=1)


def format_bytes(size: int) -> str:
    # 2**10 = 1024
    power = 2**10
    n = 0
    power_labels = {0: "", 1: "k", 2: "m", 3: "g", 4: "t"}
    # Divide as a float: integer division here would round 1536 bytes down to '1 kb'
    # and discard the fraction the format string then tries to show.
    scaled = float(size)
    while scaled > power and n < max(power_labels):
        scaled /= power
        n += 1
    return f"{int(scaled * 100) / 100} {power_labels[n]}b"


def apply_recording_timestamp(filename: str, recorded_at: datetime) -> bool:
    """Set a segment's file modification time to when the footage was recorded.

    The export endpoint stamps the MP4 it builds with the moment of the export, so a
    freshly downloaded segment of last week's footage carries this week's date. Anything
    that sorts or filters by file date -- Explorer, a media library, a retention script --
    then sees when the archive was fetched rather than when the events happened.

    Only the filesystem timestamp is changed. The bytes are untouched, so the SHA-256
    recorded in the manifest stays valid; the MP4's own internal creation_time still
    reflects the export and would need a remux to correct, which is not worth rewriting
    an archive for.
    """
    try:
        timestamp = recorded_at.timestamp()
        os.utime(filename, (timestamp, timestamp))
        return True
    except OSError as error:
        logging.warning(f"Could not set the recording time on {filename}: {error}")
        return False


def cleanup_stale_part_files(root: str) -> List[str]:
    """Delete leftover ``.part`` files under ``root`` and return what was removed.

    A partial file is worthless: it is a prefix of a segment that will be requested
    again in full. Clearing them at the start of a run stops an archive accumulating
    debris from every interrupted attempt.
    """
    removed = []
    for directory, _, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(PART_SUFFIX):
                continue
            path = os.path.join(directory, name)
            try:
                os.remove(path)
                removed.append(path)
            except OSError as error:
                logging.warning(f"Could not remove stale partial file {path}: {error}")

    if removed:
        logging.info(f"Removed {len(removed)} stale partial download(s) from a previous run")
    return removed


def make_camera_name_fs_safe(camera: Camera) -> str:
    return (
        "".join([c for c in camera.name if c.isalpha() or c.isdigit() or c == " "]).rstrip()
        + f" ({str(camera.id)[-4:]})"
    )


def print_download_stats(client: Any) -> None:
    already_archived = getattr(client, "files_already_archived", 0)
    files_total = (
        client.files_downloaded + client.files_skipped + client.files_failed + already_archived
    )
    print(
        f"{client.files_downloaded} files downloaded ({format_bytes(client.bytes_downloaded)}), "
        f"{already_archived} already archived, "
        f"{client.files_skipped} files skipped, "
        f"{client.files_failed} files failed, "
        f"{files_total} files total"
    )


def build_download_dir(
    use_subfolders: bool,
    destination_path: str,
    interval_start_tz: datetime,
    camera_name_fs_safe: str,
) -> str:
    # build file path for download
    if bool(use_subfolders):
        folder_year = interval_start_tz.strftime("%Y")
        folder_month = interval_start_tz.strftime("%m")
        folder_day = interval_start_tz.strftime("%d")

        dir_by_date_and_name = f"{folder_year}/{folder_month}/{folder_day}/{camera_name_fs_safe}"
        target_with_date_and_name = f"{destination_path}/{dir_by_date_and_name}"

        download_dir = target_with_date_and_name
        if not os.path.isdir(target_with_date_and_name):
            os.makedirs(target_with_date_and_name, exist_ok=True)
            logging.info(f"Created path {target_with_date_and_name}")
            download_dir = target_with_date_and_name
    else:
        download_dir = destination_path

    return download_dir
