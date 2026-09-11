# get camera list
import logging

from datetime import datetime
from typing import Any
from typing import List

from protect_archiver.dataclasses import Camera
from protect_archiver.downloader.camera_response import fetch_camera_data
from protect_archiver.errors import ProtectError


def get_camera_list(session: Any) -> List[Camera]:
    cameras = fetch_camera_data(session)

    camera_list = []
    for camera in cameras:
        try:
            valid = isinstance(camera["id"], str) and isinstance(camera["name"], str)
            recording_start = camera["stats"]["video"]["recordingStart"]
            valid = valid and (
                recording_start is None
                or (
                    isinstance(recording_start, (int, float))
                    and not isinstance(recording_start, bool)
                )
            )
            if not valid:
                raise ValueError
            start = (
                datetime.fromtimestamp(recording_start / 1000) if recording_start else datetime.min
            )
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            logging.error(
                "Could not load camera list: malformed camera metadata returned by Protect"
            )
            raise ProtectError(3) from None
        camera_data = Camera(id=camera["id"], name=camera["name"], recording_start=datetime.min)
        if recording_start:
            # Naive *local* time, matching the rest of the codebase: interval boundaries
            # are later turned back into epoch milliseconds with datetime.timestamp(),
            # which interprets a naive value as local. Using utcfromtimestamp here made
            # the first sync of each camera start a whole UTC offset away from the real
            # recording start. (It is also removed in Python 3.12.)
            camera_data.recording_start = start
        camera_list.append(camera_data)

    logging.info(
        "Cameras found:\n{}".format(
            "\n".join(f"- {camera.name} ({camera.id})" for camera in camera_list)
        )
    )

    return camera_list
