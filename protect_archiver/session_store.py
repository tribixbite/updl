"""Persistent storage for UniFi OS session tokens.

UniFi OS accounts backed by Ubiquiti SSO require a second factor at login. Where that
factor is delivered by email there is no shared secret the archiver could use to
authenticate on its own, so the only way to stop every run demanding a freshly emailed
code is to keep the session token a successful login returns and reuse it until it
expires. ``/api/auth/login`` is also rate limited, which makes reuse a correctness
concern rather than only a convenience.

Tokens are deliberately stored *outside* the archive destination: an archive directory
is routinely copied to external media or a NAS, and a session token is a credential.
"""

import base64
import json
import logging
import math
import os
import time

from typing import Any
from typing import Dict
from typing import Optional
from typing import TypeGuard
from typing import Union

# Treat a token as expired this many seconds before its stated expiry, so a long
# download started just under the wire does not fail mid-transfer.
EXPIRY_MARGIN_SECONDS = 300

# Used when a token carries no readable expiry claim. UniFi OS session tokens are
# typically good for far longer, but assuming a short life only costs one extra
# validation request, whereas assuming a long one costs a failed run.
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600


def _is_finite_number(value: Any) -> TypeGuard[Union[int, float]]:
    """Return whether value is a usable finite int or float, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _default_store_path() -> str:
    """Return the per-user path of the session store for this platform."""
    override = os.environ.get("PROTECT_SESSION_STORE")
    if override:
        return os.path.abspath(override)

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "protect-archiver", "sessions.json")

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "protect-archiver", "sessions.json")


def decode_jwt_expiry(token: str) -> Optional[int]:
    """Return the ``exp`` claim of a JWT as a unix timestamp, or None.

    The token is *not* verified -- it is not ours to verify, and we only want to know
    when to stop presenting it. Anything unparseable yields None so the caller can fall
    back to revalidating against the server.
    """
    try:
        payload_segment = token.split(".")[1]
    except IndexError:
        return None

    # JWT uses unpadded base64url; restore the padding before decoding.
    payload_segment += "=" * (-len(payload_segment) % 4)

    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_segment))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    expiry = payload.get("exp")
    if _is_finite_number(expiry):
        return int(expiry)
    return None


class SessionStore:
    """A small JSON-backed cache of UniFi OS session tokens, keyed by host and user."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _default_store_path()

    @staticmethod
    def _key(address: str, port: int, username: str) -> str:
        return f"{address}:{port}/{username}"

    def _read_all(self) -> Dict[str, Any]:
        if not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError) as error:
            # A corrupt or unreadable store must never be fatal: the worst case is that
            # the user is asked for a new MFA code.
            logging.warning(f"Ignoring unreadable session store at {self.path}: {error}")
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        # Create with owner-only permissions rather than chmod-ing afterwards, so the
        # token is never briefly world-readable.
        temporary_path = f"{self.path}.tmp"
        descriptor = os.open(temporary_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as fp:
                json.dump(data, fp, indent=1, sort_keys=True)
        except Exception:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
            raise
        os.replace(temporary_path, self.path)

    def load(self, address: str, port: int, username: str) -> Optional[str]:
        """Return a cached token that is still comfortably valid, else None."""
        entry = self._read_all().get(self._key(address, port, username))
        if not isinstance(entry, dict):
            return None

        token = entry.get("token")
        expires_at = entry.get("expires_at")
        if not isinstance(token, str) or not _is_finite_number(expires_at):
            return None

        remaining = expires_at - time.time()
        if remaining <= EXPIRY_MARGIN_SECONDS:
            logging.debug("Stored session token has expired or is about to")
            return None

        logging.info(f"Reusing stored session token (valid for another {int(remaining // 60)} min)")
        return token

    def save(self, address: str, port: int, username: str, token: str) -> None:
        """Persist a token, deriving its expiry from the token itself where possible."""
        expires_at = decode_jwt_expiry(token)
        if expires_at is None:
            expires_at = int(time.time()) + DEFAULT_TOKEN_LIFETIME_SECONDS
            logging.debug("Session token carries no expiry claim; assuming a short life")

        data = self._read_all()
        data[self._key(address, port, username)] = {
            "token": token,
            "expires_at": expires_at,
            "saved_at": int(time.time()),
        }
        try:
            self._write_all(data)
        except OSError as error:
            # Failing to cache a token is an inconvenience, not a reason to abort a run.
            logging.warning(f"Could not write session store at {self.path}: {error}")

    def clear(self, address: str, port: int, username: str) -> None:
        """Drop a cached token, e.g. after the server rejected it."""
        data = self._read_all()
        if data.pop(self._key(address, port, username), None) is None:
            return
        try:
            self._write_all(data)
        except OSError as error:
            logging.warning(f"Could not update session store at {self.path}: {error}")
