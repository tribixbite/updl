import json
import logging

from datetime import datetime
from os import path
from typing import List
from typing import Set

import dateutil.parser

from .client import ProtectClient
from .dataclasses import Camera
from .downloader import Downloader
from .manifest import ArchiveManifest
from .manifest import SegmentKey
from .manifest import describe_counts
from .utils import cleanup_stale_part_files
from .utils import json_encode
from .verify import LEVEL_QUICK


class ProtectSync:
    """Mirror a Protect system's footage into a local archive, resumably.

    The unit of work is an hour of one camera's recording. Every run sweeps the whole
    window the NVR still holds and asks the manifest about each hour in it, so a run
    started weeks later downloads exactly the hours that are missing or damaged --
    including ones an earlier run failed on, which the upstream statefile cursor would
    have skipped past permanently.

    Sweeping the full window rather than resuming from a cursor is also what keeps the
    archive correct as the NVR ages footage out: ``recording_start`` moves forward with
    the NVR's own retention, so the window naturally shrinks to what is still fetchable
    while everything already archived stays put.
    """

    def __init__(self, client: ProtectClient, destination_path: str, statefile: str) -> None:
        self.client = client
        self.destination_path = path.abspath(destination_path)
        self.statefile = path.abspath(path.join(destination_path, statefile))

    def readstate(self) -> dict:
        if path.isfile(self.statefile):
            with open(self.statefile) as fp:
                state = json.load(fp)
        else:
            state = {"cameras": {}}

        # The statefile is no longer authoritative, so a damaged or hand-edited one must
        # not be able to abort a run that the manifest could have driven perfectly well.
        if not isinstance(state, dict) or not isinstance(state.get("cameras"), dict):
            logging.warning(f"Ignoring malformed statefile at {self.statefile}")
            state = {"cameras": {}}

        return state

    def writestate(self, state: dict) -> None:
        with open(self.statefile, "w") as fp:
            json.dump(state, fp, default=json_encode)

    @staticmethod
    def _sweep_start(camera: Camera, state: dict) -> datetime:
        """Work out how far back this run should look for a given camera.

        ``recording_start`` is the earliest footage the NVR still holds, so it is the
        correct lower bound. A camera that has never recorded reports ``datetime.min``,
        which would otherwise expand into two million hourly requests, so fall back to
        the statefile cursor and finally to skipping the camera entirely.
        """
        recording_start = camera.recording_start
        if recording_start > datetime.min:
            return recording_start.replace(minute=0, second=0, microsecond=0)

        camera_state = state.get("cameras", {}).get(camera.id, {})
        if "last" in camera_state:
            logging.warning(
                f"Camera '{camera.name}' reports no recording start; resuming from the"
                " statefile cursor instead"
            )
            return dateutil.parser.parse(camera_state["last"]).replace(
                minute=0, second=0, microsecond=0
            )

        raise ValueError("camera has no recording start and no recorded progress")

    def run(
        self,
        camera_list: List[Camera],
        ignore_state: bool = False,
        verify_level: str = LEVEL_QUICK,
    ) -> None:
        # noinspection PyUnboundLocalVariable
        logging.info(
            f"Synchronizing video files from 'https://{self.client.address}:{self.client.port}"
        )

        # Anything left in flight by an interrupted run is a prefix of a segment that is
        # about to be requested again in full, so it is only debris.
        cleanup_stale_part_files(self.destination_path)

        state = self.readstate()

        with ArchiveManifest(self.destination_path) as manifest:
            layout_warning = manifest.check_layout(
                use_subfolders=self.client.use_subfolders,
                use_utc_filenames=self.client.use_utc_filenames,
            )
            if layout_warning:
                logging.warning(layout_warning)

            if ignore_state:
                # '--ignore-state' means re-download everything, so nothing counts as
                # already held. The existing rows stay, and are overwritten as each
                # segment is fetched again.
                logging.warning(
                    "'--ignore-state' is present - every segment will be downloaded again"
                )
                settled: Set[SegmentKey] = set()
            else:
                settled = manifest.settled_keys()
                logging.info(
                    f"Archive currently holds {len(settled)} segment(s)"
                    f" ({describe_counts(manifest.status_counts())})"
                )

            for camera in camera_list:
                try:
                    try:
                        start = self._sweep_start(camera, state)
                    except ValueError:
                        logging.warning(
                            f"Skipping camera '{camera.name}' ({camera.id}): it has no"
                            " recorded footage and no previous sync progress"
                        )
                        continue

                    # The current hour is still being written to, so stop at the last
                    # complete one and pick it up on the next run.
                    end = datetime.now().replace(minute=0, second=0, microsecond=0)
                    if start >= end:
                        logging.info(f"Camera '{camera.name}' has nothing new to archive")
                        continue

                    Downloader.download_footage(
                        self.client,
                        start,
                        end,
                        camera,
                        disable_alignment=False,
                        disable_splitting=False,
                        manifest=manifest,
                        settled=settled,
                        verify_level=verify_level,
                    )

                    state["cameras"][camera.id] = {"last": end, "name": camera.name}
                except Exception:
                    logging.exception(
                        f"Failed to sync camera {camera.name} - continuing to next device"
                    )
                finally:
                    self.writestate(state)

            counts = manifest.status_counts()
            logging.info(f"Archive now holds: {describe_counts(counts)}")

            failed = counts.get("failed", 0)
            if failed:
                logging.warning(
                    f"{failed} segment(s) could not be downloaded. They are recorded as"
                    " failed and the next run will retry them."
                )

    def __repr__(self) -> str:
        return f"<ProtectSync {self.destination_path}>"
