from collections import Counter
from os import path

import click

from protect_archiver.cli.base import cli
from protect_archiver.config import Config
from protect_archiver.manifest import STATUS_EMPTY
from protect_archiver.manifest import ArchiveManifest
from protect_archiver.manifest import describe_counts
from protect_archiver.utils import cleanup_stale_part_files
from protect_archiver.verify import LEVEL_HASH
from protect_archiver.verify import LEVEL_NONE
from protect_archiver.verify import REASON_OK
from protect_archiver.verify import VERIFY_LEVELS
from protect_archiver.verify import sha256_file
from protect_archiver.verify import verify_record


@cli.command(
    "verify",
    help=(
        "Audit a local archive against its manifest. Runs entirely offline - it never "
        "contacts the Protect system - so it is safe to run against a backup copy."
    ),
)
@click.argument("dest", type=click.Path(exists=True, writable=True, resolve_path=True))
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
    "--clean-partials",
    is_flag=True,
    default=False,
    show_default=True,
    help="Also delete leftover '.part' files from interrupted runs.",
)
def verify(
    dest: str,
    verify_level: str,
    repair: bool,
    rehash: bool,
    clean_partials: bool,
) -> None:
    dest = path.abspath(dest)

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
        failures = []

        for record in manifest.iter_segments():
            absolute_path = manifest.absolute_path(record)

            if rehash and not record.sha256 and record.status != STATUS_EMPTY:
                try:
                    digest = sha256_file(absolute_path)
                except OSError:
                    # The file is missing or unreadable; the verification below will
                    # classify it properly, so there is nothing to record here.
                    digest = ""
                if digest:
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
                    rehashed += 1

            result = verify_record(record, absolute_path, verify_level)
            reasons[result.reason] += 1

            if record.status != STATUS_EMPTY and not record.sha256:
                unhashed += 1

            if result.ok:
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
