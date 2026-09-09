import os

from collections import Counter
from datetime import datetime
from typing import Optional

import click

from protect_archiver.cli.base import cli
from protect_archiver.cli.resolve import resolve_destination
from protect_archiver.config import Config
from protect_archiver.manifest import STATUS_OK
from protect_archiver.manifest import ArchiveManifest
from protect_archiver.manifest import SegmentRecord
from protect_archiver.manifest import describe_counts
from protect_archiver.utils import apply_recording_timestamp
from protect_archiver.utils import cleanup_stale_part_files
from protect_archiver.verify import LEVEL_HASH
from protect_archiver.verify import LEVEL_NONE
from protect_archiver.verify import REASON_OK
from protect_archiver.verify import REASON_PENDING
from protect_archiver.verify import VERIFY_LEVELS
from protect_archiver.verify import sha256_file
from protect_archiver.verify import verify_record


def _store_missing_hash(
    manifest: ArchiveManifest, record: SegmentRecord, absolute_path: str
) -> int:
    """Fill in a hash for a row that has none, returning how many were stored.

    Rows adopted from disk by 'sync --reconcile' carry no hash, which leaves them
    permanently unverifiable at the hash and deep levels. Only rows that should have a
    readable file are candidates.
    """
    if record.sha256 or record.status != STATUS_OK:
        return 0

    try:
        digest = sha256_file(absolute_path)
    except OSError:
        # Missing or unreadable; the verification pass classifies it properly, so there
        # is nothing useful to record here.
        return 0

    manifest.record(
        camera_id=record.camera_id,
        camera_name=record.camera_name,
        start_ms=record.start_ms,
        end_ms=record.end_ms,
        filename=absolute_path,
        size=record.size,
        sha256=digest,
        status=record.status,
    )
    record.sha256 = digest
    return 1


def _apply_recorded_time(record: SegmentRecord, absolute_path: str) -> int:
    """Set one segment's file date to its recording time, if it is not already right."""
    recorded_at = datetime.fromtimestamp(record.start_ms / 1000)
    try:
        if int(os.path.getmtime(absolute_path)) == int(recorded_at.timestamp()):
            return 0
    except OSError:
        return 0
    return 1 if apply_recording_timestamp(absolute_path, recorded_at) else 0


@cli.command(
    "verify",
    help=(
        "Audit a local archive against its manifest. Runs entirely offline - it never "
        "contacts the Protect system - so it is safe to run against a backup copy."
    ),
)
@click.argument(
    "dest_argument",
    metavar="[DEST]",
    required=False,
    type=click.Path(exists=True, writable=True, resolve_path=True),
)
@click.option(
    "-d",
    "--dest",
    "dest_option",
    required=False,
    type=click.Path(exists=True, writable=True, resolve_path=True),
    help="Archive to audit. Defaults to the destination remembered from the last sync.",
    envvar="PROTECT_DEST",
    show_envvar=True,
)
@click.option(
    "--level",
    "verify_level",
    type=click.Choice([level for level in VERIFY_LEVELS if level != LEVEL_NONE]),
    default=Config.VERIFY_LEVEL,
    show_default=True,
    help=(
        "'quick' checks each file exists at its recorded size, 'hash' re-computes its "
        "SHA-256, and 'deep' additionally asks ffprobe to decode it."
    ),
    envvar="PROTECT_VERIFY_LEVEL",
    show_envvar=True,
)
@click.option(
    "--repair",
    is_flag=True,
    default=False,
    show_default=True,
    help=(
        "Mark every segment that fails verification for re-download, so the next sync "
        "fetches it again. Without this the audit only reports."
    ),
)
@click.option(
    "--rehash",
    is_flag=True,
    default=False,
    show_default=True,
    help=(
        "Compute and store a SHA-256 for segments that have none, such as those adopted "
        "from disk by 'sync --reconcile'. Reads every such file."
    ),
)
@click.option(
    "--require-audio",
    is_flag=True,
    default=False,
    show_default=True,
    help=(
        "Flag segments that have no audio track. UniFi Protect silently omits audio for "
        "an account without 'readmedia' permission on the camera, so an archive can look "
        "complete while every clip is silent. Needs ffprobe. Combine with --repair to "
        "re-fetch them."
    ),
)
@click.option(
    "--fix-timestamps",
    is_flag=True,
    default=False,
    show_default=True,
    help=(
        "Set each file's modification time to when its footage was recorded. The export "
        "endpoint stamps files with the moment of export, so an archive fetched today "
        "carries today's date for footage from last week. Content is untouched, so "
        "recorded hashes stay valid."
    ),
)
@click.option(
    "--clean-partials",
    is_flag=True,
    default=False,
    show_default=True,
    help="Also delete leftover '.part' files from interrupted runs.",
)
def verify(
    dest_argument: Optional[str],
    dest_option: Optional[str],
    verify_level: str,
    repair: bool,
    rehash: bool,
    require_audio: bool,
    fix_timestamps: bool,
    clean_partials: bool,
) -> None:
    dest = resolve_destination(
        dest_argument,
        dest_option,
        "updl verify -d /srv/protect",
    )

    if clean_partials:
        cleanup_stale_part_files(dest)

    with ArchiveManifest(dest) as manifest:
        counts = manifest.status_counts()
        if not counts:
            click.echo(
                f"No manifest entries found under {dest}. If this archive was built by an "
                "older version, run 'sync --reconcile' once to adopt what is on disk."
            )
            return

        click.echo(f"Archive: {dest}")
        click.echo(f"Manifest: {describe_counts(counts)}")
        click.echo(f"Verifying at level '{verify_level}'...")

        reasons: Counter = Counter()
        unhashed = 0
        repaired = 0
        rehashed = 0
        retimed = 0
        failures = []

        for record in manifest.iter_segments():
            absolute_path = manifest.absolute_path(record)

            if rehash:
                rehashed += _store_missing_hash(manifest, record, absolute_path)

            if fix_timestamps and record.status == STATUS_OK:
                retimed += _apply_recorded_time(record, absolute_path)

            result = verify_record(record, absolute_path, verify_level, require_audio)
            reasons[result.reason] += 1

            if record.status == STATUS_OK and not record.sha256:
                unhashed += 1

            if result.ok:
                # A row awaiting re-download has no file to have been verified, so
                # stamping it as verified would be a lie.
                if result.reason != REASON_PENDING:
                    manifest.mark_verified(record)
                continue

            failures.append((record, result))
            if repair:
                manifest.mark_for_redownload(record)
                repaired += 1

        total = sum(reasons.values())
        click.echo("")
        click.echo(f"Checked {total} segment(s):")
        for reason, count in sorted(reasons.items()):
            label = "intact" if reason == REASON_OK else reason
            click.echo(f"  {count:>8}  {label}")

        pending = reasons.get(REASON_PENDING, 0)
        if pending:
            click.echo(
                f"\n{pending} segment(s) have never downloaded successfully and are already"
                " queued for another attempt. That is a record of gaps, not damage - run"
                " 'sync' to retry them."
            )

        if rehashed:
            click.echo(f"\nStored {rehashed} newly computed hash(es)")

        if unhashed and verify_level in (LEVEL_HASH, "deep"):
            click.echo(
                f"\n{unhashed} segment(s) carry no recorded hash and could not be checked "
                "for silent corruption. Re-run with --rehash to record one for each."
            )

        if failures:
            click.echo(f"\n{len(failures)} segment(s) failed verification:")
            for record, result in failures[:25]:
                click.echo(f"  {record.path}: {result}")
            if len(failures) > 25:
                click.echo(f"  ... and {len(failures) - 25} more")

            if repair:
                click.echo(
                    f"\nMarked {repaired} segment(s) for re-download. Run 'sync' to fetch them."
                )
            else:
                click.echo("\nRe-run with --repair to have the next sync fetch these again.")
            raise SystemExit(1)

        click.echo("\nEvery segment in the manifest verified successfully.")
