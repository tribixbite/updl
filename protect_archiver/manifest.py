"""A durable record of what the archive already holds.

The upstream tool decides what to fetch from a statefile holding one cursor per camera.
That is enough to resume an interrupted run, but not enough to re-run the tool weeks
later and have it fetch only what is genuinely missing: the cursor advances past hours
that failed, so a gap once created is never revisited, and nothing on disk is ever
checked against what was supposed to be written.

This module keeps a per-archive SQLite manifest instead, one row per downloaded segment,
recording where the file went, how big it was and what it hashed to. That makes three
things possible which the cursor cannot express:

* re-running later skips only segments that are actually present and intact;
* hours that failed, or that the NVR reported as empty, are remembered as such, so
  failures can be retried and empty hours are never requested twice;
* the archive can be audited offline, without contacting the NVR at all.

SQLite rather than a JSON document because a four-camera year is roughly 35,000 rows per
camera, and rewriting a JSON blob once per downloaded segment is quadratic.
"""

import logging
import os
import sqlite3

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from types import TracebackType
from typing import Any
from typing import Dict
from typing import Iterator
from typing import Optional
from typing import Set
from typing import Tuple
from typing import Type


# Subdirectory of the archive holding archiver bookkeeping. Kept dot-prefixed and in one
# place so a user browsing the archive sees footage, not machinery.
METADATA_DIRNAME = ".protect-archive"
MANIFEST_FILENAME = "manifest.db"

SCHEMA_VERSION = "1"

# Segment outcomes.
STATUS_OK = "ok"
STATUS_EMPTY = "empty"  # NVR held no footage for this hour; never worth re-requesting
STATUS_FAILED = "failed"  # download failed; a later run should retry exactly this

# Statuses that mean "do not download this again".
SETTLED_STATUSES = (STATUS_OK, STATUS_EMPTY)

# A segment is identified by its camera and the instant it starts. The end is stored but
# deliberately not part of the identity: a sync always requests hour-aligned ranges, so
# the start pins the segment, and keeping the key to two fields is what lets an archive
# whose manifest was lost be rebuilt from the filenames on disk.
SegmentKey = Tuple[str, int]


@dataclass
class SegmentRecord:
    """One archived time range for one camera."""

    camera_id: str
    camera_name: str
    start_ms: int
    end_ms: int
    path: str
    size: int
    sha256: str
    status: str
    downloaded_at: str
    verified_at: Optional[str]


def utc_now_iso() -> str:
    """Timestamps in the manifest are UTC, so an archive stays readable across a move."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def metadata_dir(destination_path: str) -> str:
    return os.path.join(os.path.abspath(destination_path), METADATA_DIRNAME)


def manifest_path(destination_path: str) -> str:
    return os.path.join(metadata_dir(destination_path), MANIFEST_FILENAME)


def to_archive_relative(destination_path: str, filename: str) -> str:
    """Store paths relative to the archive root, with forward slashes.

    An archive should survive being moved between drives or hosts, so an absolute
    ``D:\\Unifi\\...`` path would make the manifest wrong the moment the volume letter
    changes. Separators are normalised so a manifest written on Windows still resolves
    when the archive is read on Linux.
    """
    absolute_root = os.path.abspath(destination_path)
    absolute_file = os.path.abspath(filename)
    try:
        relative = os.path.relpath(absolute_file, absolute_root)
    except ValueError:
        # Different drive letters on Windows; fall back to the absolute path so the
        # record is still meaningful even though it is not portable.
        relative = absolute_file
    return relative.replace(os.sep, "/")


def to_absolute(destination_path: str, archive_relative: str) -> str:
    return os.path.join(os.path.abspath(destination_path), archive_relative.replace("/", os.sep))


class ArchiveManifest:
    """SQLite-backed index of an archive's contents."""

    def __init__(self, destination_path: str) -> None:
        self.destination_path = os.path.abspath(destination_path)
        self.path = manifest_path(self.destination_path)
        os.makedirs(metadata_dir(self.destination_path), exist_ok=True)

        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        # WAL keeps a reader (an audit) from blocking the writer (a running sync), and
        # survives an abrupt process kill without corrupting the index.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS segments (
                    camera_id     TEXT    NOT NULL,
                    camera_name   TEXT    NOT NULL,
                    start_ms      INTEGER NOT NULL,
                    end_ms        INTEGER NOT NULL,
                    path          TEXT    NOT NULL,
                    size          INTEGER NOT NULL,
                    sha256        TEXT    NOT NULL,
                    status        TEXT    NOT NULL,
                    downloaded_at TEXT    NOT NULL,
                    verified_at   TEXT,
                    PRIMARY KEY (camera_id, start_ms)
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_segments_status ON segments (status)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_segments_camera ON segments (camera_id, start_ms)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    # -- metadata ---------------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        row = self._connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def check_layout(self, use_subfolders: bool, use_utc_filenames: bool) -> Optional[str]:
        """Compare this run's layout options against the archive's, and remember them.

        Changing either option changes the path a segment lands at without changing its
        identity, so a run with different options would quietly build a second, parallel
        copy of the whole archive. Returning a warning lets the caller say so loudly
        rather than filling the disk twice.
        """
        desired = {
            "use_subfolders": str(bool(use_subfolders)),
            "use_utc_filenames": str(bool(use_utc_filenames)),
        }

        differences = []
        for key, value in desired.items():
            existing = self.get_meta(key)
            if existing is None:
                self.set_meta(key, value)
            elif existing != value:
                differences.append(
                    f"{key}: archive was built with {existing}, this run uses {value}"
                )

        if not differences:
            return None
        return (
            "This archive's layout does not match the current options ("
            + "; ".join(differences)
            + "). Continuing would download a second copy alongside the existing one."
        )

    # -- reads ------------------------------------------------------------------

    @staticmethod
    def _to_record(row: sqlite3.Row) -> SegmentRecord:
        return SegmentRecord(
            camera_id=row["camera_id"],
            camera_name=row["camera_name"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            path=row["path"],
            size=row["size"],
            sha256=row["sha256"],
            status=row["status"],
            downloaded_at=row["downloaded_at"],
            verified_at=row["verified_at"],
        )

    def get(self, camera_id: str, start_ms: int) -> Optional[SegmentRecord]:
        row = self._connection.execute(
            "SELECT * FROM segments WHERE camera_id = ? AND start_ms = ?",
            (camera_id, start_ms),
        ).fetchone()
        return self._to_record(row) if row else None

    def settled_keys(self) -> Set[SegmentKey]:
        """Every segment that needs no further download, loaded once per run.

        A sync sweeps the whole retention window on each run so that gaps get filled.
        Holding the settled set in memory turns each of those checks into a dictionary
        lookup instead of a query, which matters when the sweep is tens of thousands of
        intervals long.
        """
        placeholders = ", ".join("?" for _ in SETTLED_STATUSES)
        rows = self._connection.execute(
            f"SELECT camera_id, start_ms FROM segments WHERE status IN ({placeholders})",
            SETTLED_STATUSES,
        ).fetchall()
        return {(row["camera_id"], row["start_ms"]) for row in rows}

    def iter_segments(self, status: Optional[str] = None) -> Iterator[SegmentRecord]:
        if status is None:
            cursor = self._connection.execute("SELECT * FROM segments ORDER BY camera_id, start_ms")
        else:
            cursor = self._connection.execute(
                "SELECT * FROM segments WHERE status = ? ORDER BY camera_id, start_ms", (status,)
            )
        for row in cursor:
            yield self._to_record(row)

    def status_counts(self) -> Dict[str, int]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS total FROM segments GROUP BY status"
        ).fetchall()
        return {row["status"]: row["total"] for row in rows}

    # -- writes -----------------------------------------------------------------

    def record(
        self,
        camera_id: str,
        camera_name: str,
        start_ms: int,
        end_ms: int,
        filename: str,
        size: int,
        sha256: str,
        status: str,
        verified_at: Optional[str] = None,
    ) -> None:
        """Insert or replace the record for one segment."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO segments (camera_id, camera_name, start_ms, end_ms, path, size,
                                      sha256, status, downloaded_at, verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(camera_id, start_ms) DO UPDATE SET
                    camera_name   = excluded.camera_name,
                    end_ms        = excluded.end_ms,
                    path          = excluded.path,
                    size          = excluded.size,
                    sha256        = excluded.sha256,
                    status        = excluded.status,
                    downloaded_at = excluded.downloaded_at,
                    verified_at   = excluded.verified_at
                """,
                (
                    camera_id,
                    camera_name,
                    start_ms,
                    end_ms,
                    to_archive_relative(self.destination_path, filename),
                    size,
                    sha256,
                    status,
                    utc_now_iso(),
                    verified_at,
                ),
            )

    def mark_verified(self, record: SegmentRecord) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE segments SET verified_at = ? WHERE camera_id = ? AND start_ms = ?",
                (utc_now_iso(), record.camera_id, record.start_ms),
            )

    def mark_for_redownload(self, record: SegmentRecord) -> None:
        """Demote a segment so the next sync fetches it again."""
        with self._connection:
            self._connection.execute(
                "UPDATE segments SET status = ?, verified_at = NULL "
                "WHERE camera_id = ? AND start_ms = ?",
                (STATUS_FAILED, record.camera_id, record.start_ms),
            )

    def absolute_path(self, record: SegmentRecord) -> str:
        return to_absolute(self.destination_path, record.path)

    # -- lifecycle --------------------------------------------------------------

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error as error:
            logging.warning(f"Error closing manifest at {self.path}: {error}")

    def __enter__(self) -> "ArchiveManifest":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<ArchiveManifest {self.path}>"


def open_manifest(destination_path: str) -> ArchiveManifest:
    return ArchiveManifest(destination_path)


def describe_counts(counts: Dict[str, Any]) -> str:
    if not counts:
        return "empty"
    return ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
