"""Rebuild manifest rows from footage that is already on disk.

The manifest is what stops a re-run downloading everything again, which makes losing it
-- a deleted `.protect-archive` directory, an archive copied without its hidden folder,
or footage fetched by a version of this tool that predates it -- expensive: terabytes
would be pulled from the NVR a second time to recreate knowledge the filenames already
carry.

Adoption reads that knowledge back. A segment's file name encodes the camera and the
instant the segment starts, which is exactly the manifest's identity for it, so every
orphan file can be matched to the camera it came from and recorded as held.

Two limits are worth stating plainly. The end of a segment is not recoverable from its
name, so an adopted row assumes the hour-aligned end that a sync always requests; this
is informational only, since the end is not part of a segment's identity. And adopted
rows carry no hash, because hashing a multi-terabyte archive is not something to do
implicitly -- ``verify --rehash`` fills them in when that is wanted.
"""

import logging
import os
import re

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional

from protect_archiver.dataclasses import Camera
from protect_archiver.manifest import METADATA_DIRNAME
from protect_archiver.manifest import STATUS_OK
from protect_archiver.manifest import ArchiveManifest
from protect_archiver.utils import make_camera_name_fs_safe

# Mirrors the name built in download_footage: '<camera> (<id suffix>) - <date> - <time>[tz].mp4'
SEGMENT_FILENAME_PATTERN = re.compile(
    r"^(?P<camera>.+?) - "
    r"(?P<date>\d{4}-\d{2}-\d{2}) - "
    r"(?P<time>\d{2}\.\d{2}\.\d{2})"
    r"(?P<offset>[+-]\d{4})?"
    r"\.mp4$"
)

# A sync requests whole hours, so an adopted segment is assumed to cover one.
ASSUMED_SEGMENT_DURATION = timedelta(hours=1)


@dataclass
class ReconcileReport:
    adopted: int = 0
    already_known: int = 0
    unmatched: List[str] = field(default_factory=list)


def parse_segment_filename(name: str) -> Optional[datetime]:
    """Return the start time encoded in a segment's file name, if it is one."""
    match = SEGMENT_FILENAME_PATTERN.match(name)
    if not match:
        return None

    stamp = f"{match.group('date')} {match.group('time')}"
    offset = match.group("offset")
    try:
        if offset:
            return datetime.strptime(f"{stamp}{offset}", "%Y-%m-%d %H.%M.%S%z")
        return datetime.strptime(stamp, "%Y-%m-%d %H.%M.%S")
    except ValueError:
        return None


def parse_camera_label(name: str) -> Optional[str]:
    """Return the '<camera> (<id suffix>)' portion of a segment file name."""
    match = SEGMENT_FILENAME_PATTERN.match(name)
    return match.group("camera") if match else None


def build_camera_index(cameras: Iterable[Camera]) -> Dict[str, Camera]:
    """Map the filesystem-safe label used in file names back to its camera."""
    return {make_camera_name_fs_safe(camera): camera for camera in cameras}


def find_orphans(
    destination_path: str,
    manifest: ArchiveManifest,
    cameras: Iterable[Camera],
) -> ReconcileReport:
    """Walk the archive and record any segment file the manifest does not know about."""
    camera_index = build_camera_index(cameras)
    settled = manifest.settled_keys()
    report = ReconcileReport()

    root = os.path.abspath(destination_path)
    for directory, subdirectories, filenames in os.walk(root):
        # Never descend into the archiver's own bookkeeping directory.
        subdirectories[:] = [name for name in subdirectories if name != METADATA_DIRNAME]

        for name in filenames:
            if not name.endswith(".mp4"):
                continue

            full_path = os.path.join(directory, name)
            label = parse_camera_label(name)
            start = parse_segment_filename(name)

            if label is None or start is None:
                report.unmatched.append(full_path)
                continue

            camera = camera_index.get(label)
            if camera is None:
                # The file belongs to a camera this system no longer reports, so its id
                # cannot be recovered and the row cannot be keyed.
                report.unmatched.append(full_path)
                continue

            start_ms = int(start.timestamp() * 1e3)
            if (camera.id, start_ms) in settled:
                report.already_known += 1
                continue

            end_ms = int((start + ASSUMED_SEGMENT_DURATION).timestamp() * 1e3) - 1
            manifest.record(
                camera_id=camera.id,
                camera_name=camera.name,
                start_ms=start_ms,
                end_ms=end_ms,
                filename=full_path,
                size=os.path.getsize(full_path),
                # Left empty deliberately: see the module docstring.
                sha256="",
                status=STATUS_OK,
            )
            settled.add((camera.id, start_ms))
            report.adopted += 1

    logging.info(
        f"Adopted {report.adopted} existing file(s) into the manifest,"
        f" {report.already_known} were already recorded,"
        f" {len(report.unmatched)} could not be matched to a camera"
    )
    for path in report.unmatched[:20]:
        logging.debug(f"Unmatched file: {path}")

    return report
