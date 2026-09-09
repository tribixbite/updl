from os import path
from typing import Optional

import click

from protect_archiver import settings
from protect_archiver.cli.base import cli
from protect_archiver.cli.resolve import resolve_destination
from protect_archiver.client import ProtectClient
from protect_archiver.config import Config
from protect_archiver.manifest import ArchiveManifest
from protect_archiver.reconcile import find_orphans
from protect_archiver.sync import ProtectSync
from protect_archiver.utils import print_download_stats
from protect_archiver.verify import VERIFY_LEVELS


@cli.command("sync", help="Synchronize your UniFi Protect footage to a local destination")
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
    help=(
        "Archive destination. May also be given as a positional argument. Remembered "
        "between runs, so later runs can omit it."
    ),
    envvar="PROTECT_DEST",
    show_envvar=True,
)
@click.option(
    "-a",
    "--address",
    default=Config.ADDRESS,
    show_default=True,
    required=True,
    help="IP address or hostname of the UniFi Protect Server",
    envvar="PROTECT_ADDRESS",
    show_envvar=True,
)
@click.option(
    "--port",
    default=Config.PORT,
    show_default=True,
    required=False,
    help="The port of the UniFi Protect Server",
    envvar="PROTECT_PORT",
    show_envvar=True,
)
@click.option(
    "--not-unifi-os",
    is_flag=True,
    default=False,
    show_default=True,
    help="Use this for systems without UniFi OS",
    envvar="PROTECT_NOT_UNIFI_OS",
    show_envvar=True,
)
@click.option(
    "-u",
    "--username",
    required=True,
    help="Username of user with local access",
    prompt="Username of local Protect user",
    envvar="PROTECT_USERNAME",
    show_envvar=True,
)
@click.option(
    "-p",
    "--password",
    required=True,
    help="Password of user with local access",
    prompt="Password for local Protect user",
    hide_input=True,
    envvar="PROTECT_PASSWORD",
    show_envvar=True,
)
@click.option(
    "--mfa-code",
    required=False,
    default=None,
    help=(
        "One-time multi-factor code, for accounts backed by Ubiquiti SSO. "
        "If omitted and a code is required, it is prompted for. The resulting session "
        "is cached, so this is normally only needed occasionally."
    ),
    envvar="PROTECT_MFA_CODE",
    show_envvar=True,
)
@click.option(
    "--no-session-store",
    is_flag=True,
    default=False,
    show_default=True,
    help=(
        "Do not cache the session token between runs. Every run will then need a fresh "
        "login, and a fresh multi-factor code if the account requires one."
    ),
    envvar="PROTECT_NO_SESSION_STORE",
    show_envvar=True,
)
@click.option(
    "--verify-ssl",
    is_flag=True,
    default=False,
    show_default=True,
    help="Verify Protect SSL certificate",
    envvar="PROTECT_VERIFY_SSL",
    show_envvar=True,
)
@click.option(
    "--cameras",
    default="all",
    show_default=True,
    help=(
        "Comma-separated list of one or more camera IDs ('--cameras=\"id_1,id_2,id_3,...\"'). "
        "Use '--cameras=all' to download footage of all available cameras."
    ),
    envvar="PROTECT_CAMERAS",
    show_envvar=True,
)
@click.option(
    "--ignore-failed-downloads",
    is_flag=True,
    default=False,
    show_default=True,
    help="Ignore failed downloads and continue with next download",
    envvar="PROTECT_IGNORE_FAILED_DOWNLOADS",
    show_envvar=True,
)
@click.option(
    "--use-utc-filenames",
    is_flag=True,
    default=False,
    show_default=True,
    help="Use UTC timestamp in file names instead of local time",
    envvar="PROTECT_USE_UTC",
    show_envvar=True,
)
@click.option(
    "--skip-existing-files",
    is_flag=True,
    default=False,
    show_default=True,
    help=(
        "Skip downloading files which already exist on disk, without consulting the "
        "archive manifest. The manifest already prevents re-downloads, so this is only "
        "useful for adopting an archive built by an older version."
    ),
    envvar="PROTECT_SKIP_EXISTING",
    show_envvar=True,
)
@click.option(
    "--wait-between-downloads",
    "download_wait",
    default=Config.DOWNLOAD_WAIT,
    show_default=True,
    help="Time to wait between file downloads, in seconds",
    envvar="PROTECT_WAIT_BETWEEN_DOWNLOADS",
    show_envvar=True,
)
@click.option(
    "--download-request-timeout",
    "download_timeout",
    default=Config.DOWNLOAD_TIMEOUT,
    show_default=True,
    help="Time to wait before aborting download request, in seconds",
    envvar="PROTECT_DOWNLOAD_TIMEOUT",
    show_envvar=True,
)
@click.option(
    "--max-retries",
    default=Config.MAX_RETRIES,
    show_default=True,
    help="How many times to attempt a segment before recording it as failed",
    envvar="PROTECT_MAX_RETRIES",
    show_envvar=True,
)
@click.option(
    "-v",
    "--verify",
    "verify_level",
    type=click.Choice(VERIFY_LEVELS),
    default=Config.VERIFY_LEVEL,
    show_default=True,
    help=(
        "How thoroughly to check a file the archive already holds before skipping it. "
        "'none' trusts the manifest, 'quick' checks the file exists at the recorded "
        "size, 'hash' re-computes its SHA-256, and 'deep' additionally asks ffprobe to "
        "decode it. Anything that fails is downloaded again."
    ),
    envvar="PROTECT_VERIFY_LEVEL",
    show_envvar=True,
)
@click.option(
    "--statefile",
    default="sync.state",
    show_default=True,
    envvar="PROTECT_SYNC_STATEFILE",
    show_envvar=True,
)
@click.option(
    "--ignore-state",
    is_flag=True,
    default=False,
    show_default=True,
    help="Re-download every segment, ignoring what the archive already holds",
    envvar="PROTECT_SYNC_IGNORE_STATE",
    show_envvar=True,
)
@click.option(
    "--reconcile",
    is_flag=True,
    default=False,
    show_default=True,
    help=(
        "Before syncing, adopt footage already on disk that the manifest does not know "
        "about. Use this once after upgrading an archive built by an older version, or "
        "after losing the manifest, to avoid re-downloading what is already held."
    ),
    envvar="PROTECT_SYNC_RECONCILE",
    show_envvar=True,
)
def sync(
    dest_argument: Optional[str],
    dest_option: Optional[str],
    address: str,
    port: int,
    not_unifi_os: bool,
    username: str,
    password: str,
    mfa_code: Optional[str],
    no_session_store: bool,
    verify_ssl: bool,
    statefile: str,
    ignore_state: bool,
    ignore_failed_downloads: bool,
    cameras: str,
    use_utc_filenames: bool,
    skip_existing_files: bool,
    download_wait: int,
    download_timeout: float,
    max_retries: int,
    verify_level: str,
    reconcile: bool,
) -> None:
    dest = resolve_destination(dest_argument, dest_option, "updl -d /srv/protect")
    if not path.isdir(dest):
        click.echo(f"Video file destination directory '{dest} is invalid or does not exist!")
        exit(1)

    client = ProtectClient(
        address=address,
        port=port,
        not_unifi_os=not_unifi_os,
        username=username,
        password=password,
        mfa_code=mfa_code,
        use_session_store=not no_session_store,
        verify_ssl=verify_ssl,
        destination_path=dest,
        ignore_failed_downloads=ignore_failed_downloads,
        use_subfolders=True,
        use_utc_filenames=use_utc_filenames,
        skip_existing_files=skip_existing_files,
        download_wait=download_wait,
        download_timeout=download_timeout,
        max_retries=max_retries,
    )

    # get camera list
    print("Getting camera list")
    camera_list = client.get_camera_list()

    if cameras != "all":
        camera_ids = set(cameras.split(","))
        camera_list = [c for c in camera_list if c.id in camera_ids]

    if reconcile:
        # Adoption needs the live camera list to turn the id suffix in each file name
        # back into a full camera id, which is why it lives here and not in 'verify'.
        click.echo("Adopting existing files into the manifest")
        with ArchiveManifest(dest) as manifest:
            report = find_orphans(dest, manifest, camera_list)
        click.echo(
            f"Adopted {report.adopted}, already recorded {report.already_known},"
            f" unmatched {len(report.unmatched)}"
        )

    process = ProtectSync(client=client, destination_path=dest, statefile=statefile)
    process.run(camera_list, ignore_state=ignore_state, verify_level=verify_level)

    print_download_stats(client)

    # Remember what identifies this job so a later run needs no arguments. Credentials
    # are never included; see protect_archiver.settings.
    settings.save(
        {
            "address": address,
            "port": port,
            "not_unifi_os": not_unifi_os,
            "username": username,
            "dest": dest,
            "cameras": cameras,
            "verify_level": verify_level,
            "use_utc_filenames": use_utc_filenames,
            "download_wait": download_wait,
            "download_timeout": download_timeout,
            "max_retries": max_retries,
            "statefile": statefile,
        }
    )
