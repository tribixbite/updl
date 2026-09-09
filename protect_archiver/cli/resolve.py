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
            f"No archive destination given and none remembered. "
            f"Pass it as an argument or with -d/--dest, for example: {example}"
        )
    return path.abspath(destination)
