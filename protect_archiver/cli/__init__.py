from typing import List
from typing import Sequence
from typing import Set

from .download import *  # NOQA
from .events import *  # NOQA
from .sync import *  # NOQA
from .verify import *  # NOQA


def main() -> None:
    import logging
    import os

    import urllib3

    logging.basicConfig(format="%(message)s", level=logging.INFO)

    os.environ.setdefault("PYTHONUNBUFFERED", "true")

    # disable InsecureRequestWarning for unverified HTTPS requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    import sys

    from protect_archiver.errors import ProtectError

    from .base import cli

    try:
        cli.main(args=resolve_argv(sys.argv[1:], set(cli.commands)))
    except ProtectError as error:
        # Command implementations log the useful diagnostic before raising. Converting
        # the domain error here keeps every command's exit behaviour consistent and
        # avoids showing users an internal traceback for an expected operational error.
        raise SystemExit(error.code) from None


def resolve_argv(argv: Sequence[str], commands: Set[str]) -> List[str]:
    """Make sync the primary command, with ``sync`` retained as an explicit alias.

    ``updl DEST`` and ``updl [OPTIONS]`` are the normal sync forms. Anything that names
    another sub-command is left alone, as is top-level help. Keeping the explicit
    ``sync`` command preserves existing scripts and schedules.
    """
    if not argv:
        return ["sync"]
    if argv[0] in commands or argv[0] in ("--help", "-h"):
        return list(argv)
    return ["sync", *argv]
