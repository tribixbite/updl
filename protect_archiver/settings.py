"""Remembered options, so a second run does not have to repeat the first.

Archiving is a standing job: the console address, the account and the destination do not
change between runs, and retyping them is both tedious and a source of mistakes -- a typo
in the destination silently starts a second archive somewhere else.

So a successful run writes those back, and later runs fall back to them. What is stored is
deliberately limited to the things that identify the job, never the credentials that
authorise it: **the password is never written here**. It is only needed when the cached
session token has expired, and prompting for it then is a far better trade than keeping it
on disk.

Precedence, highest first: an explicit command-line option, the matching ``PROTECT_*``
environment variable, the value remembered here, then the built-in default. This is
implemented through Click's own ``default_map``, so options behave normally and ``--help``
still shows where each value came from.
"""

import json
import logging
import os

from typing import Any
from typing import Dict
from typing import Optional

# Only these are remembered. Anything absent from this list -- passwords, one-time codes,
# --ignore-state, --reconcile -- is deliberately not persisted, either because it is a
# secret or because silently repeating it on a later run would be surprising.
REMEMBERED_KEYS = (
    "address",
    "port",
    "not_unifi_os",
    "username",
    "dest",
    "cameras",
    "verify_level",
    "use_utc_filenames",
    "download_wait",
    "download_timeout",
    "max_retries",
    "statefile",
)

# Click matches a default_map by *parameter name*, not by option string, so the stored
# key has to be translated where the two differ.
PARAMETER_NAMES = {"dest": "dest_option"}

# What each command may take from the remembered settings. 'verify' gets only the
# destination: it has its own narrower set of --verify levels, and inheriting sync's
# would let a stored 'none' become an invalid choice for it.
COMMAND_DEFAULTS = {
    "sync": REMEMBERED_KEYS,
    "verify": ("dest",),
}

CONFIG_DIRNAME = "updl"
CONFIG_FILENAME = "config.json"


def config_path() -> str:
    """Return the per-user configuration file path for this platform."""
    override = os.environ.get("UPDL_CONFIG")
    if override:
        return os.path.abspath(override)

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, CONFIG_DIRNAME, CONFIG_FILENAME)

    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, CONFIG_DIRNAME, CONFIG_FILENAME)


def load() -> Dict[str, Any]:
    """Read remembered settings, tolerating a missing or damaged file."""
    path = config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError) as error:
        # Never let a bad config file stop a run; the worst case is being asked for
        # options that would otherwise have been remembered.
        logging.warning(f"Ignoring unreadable settings at {path}: {error}")
        return {}
    return data if isinstance(data, dict) else {}


def save(values: Dict[str, Any]) -> None:
    """Remember the identifying options of a run, dropping anything secret or empty."""
    remembered = {
        key: value
        for key, value in values.items()
        if key in REMEMBERED_KEYS and value is not None and value != ""
    }
    if not remembered:
        return

    existing = load()
    if existing == remembered:
        return

    path = config_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    temporary_path = f"{path}.tmp"
    try:
        # Owner-only from creation: the file names the archive and the account, which is
        # not secret but is nobody else's business either.
        descriptor = os.open(temporary_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as fp:
            json.dump(remembered, fp, indent=1, sort_keys=True)
        os.replace(temporary_path, path)
    except OSError as error:
        logging.warning(f"Could not save settings to {path}: {error}")
        if os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                pass
        return

    logging.debug(f"Remembered settings in {path}")


def as_default_map() -> Dict[str, Dict[str, Any]]:
    """Shape the remembered settings as a Click ``default_map``.

    Click consults this only when an option was not supplied on the command line or via
    its environment variable, which gives exactly the precedence this module documents.
    """
    remembered = load()
    if not remembered:
        return {}

    default_map: Dict[str, Dict[str, Any]] = {}
    for command, allowed in COMMAND_DEFAULTS.items():
        values = {
            PARAMETER_NAMES.get(key, key): value
            for key, value in remembered.items()
            if key in allowed
        }
        if values:
            default_map[command] = values
    return default_map


def describe() -> Optional[str]:
    """A one-line summary of what is remembered, for the CLI to echo."""
    remembered = load()
    if not remembered:
        return None
    parts = [
        f"{key}={remembered[key]}" for key in ("address", "username", "dest") if key in remembered
    ]
    return ", ".join(parts) if parts else None
