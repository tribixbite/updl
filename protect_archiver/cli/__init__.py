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

    from .base import cli

    cli.main(args=resolve_argv(sys.argv[1:], set(cli.commands)))


def resolve_argv(argv: Sequence[str], commands: Set[str]) -> List[str]:
    """Let `updl` with no sub-command mean `updl sync`.

    A machine that has run once before has its address, account and destination
    remembered, so the useful default for a bare invocation is to get on with the sync.
    Anything that already names a sub-command is left alone, and so is the top-level
    help, which must not turn into `sync --help`.
    """
    if not argv:
        return ["sync"]
    if argv[0] in commands or argv[0] in ("--help", "-h"):
        return list(argv)
    return ["sync", *argv]
