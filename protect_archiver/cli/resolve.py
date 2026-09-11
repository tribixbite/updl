"""Working out which archive a command should act on."""

from os import path
from typing import Optional

import click


def resolve_destination(
    dest_argument: Optional[str],
    dest_option: Optional[str],
    example: str,
) -> str:
    """Pick the archive directory from the argument, the option, or what is remembered.

    A positional ``DEST`` wins over ``-d``, and either wins over the value a previous run
    remembered, which Click has already applied to ``dest_option`` as a default. Failing
    with an example beats failing with a usage dump, because the usual cause is a first
    run on a machine that has nothing remembered yet.
    """
    destination = dest_argument or dest_option
    if not destination:
        raise click.UsageError(
            "No archive destination given and none remembered.\n\n"
            "To start an archive sync, specify the destination and Protect console address:\n"
            "  updl -a <protect-ip> -u <username> <destination>\n\n"
            f"For example:\n"
            f"  {example}\n\n"
            "Options can also be set via environment variables (PROTECT_ADDRESS, PROTECT_DEST, etc.).\n"
            "Once a sync succeeds, settings are remembered so future runs can be started with plain 'updl'.\n"
            "Run 'updl --help' to see all available options."
        )
    return path.abspath(destination)
