import base64
import json
import time

from typing import Any

from protect_archiver.session_store import EXPIRY_MARGIN_SECONDS
from protect_archiver.session_store import SessionStore
from protect_archiver.session_store import decode_jwt_expiry


def make_token(expires_at: int) -> str:
    """Build a JWT-shaped token whose payload carries an expiry, as UniFi OS returns."""
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": expires_at}).encode()).decode().rstrip("=")
    )
    return f"header.{payload}.signature"


def test_decode_jwt_expiry_reads_the_claim() -> None:
    assert decode_jwt_expiry(make_token(1788830877)) == 1788830877


def test_decode_jwt_expiry_tolerates_rubbish() -> None:
    # A token the archiver cannot read must not crash a run; it falls back to a
    # conservative assumed lifetime instead.
    assert decode_jwt_expiry("not.a.jwt") is None
    assert decode_jwt_expiry("nodots") is None
    assert decode_jwt_expiry("") is None


def test_round_trips_a_live_token(tmp_path: Any) -> None:
    store = SessionStore(str(tmp_path / "sessions.json"))
    token = make_token(int(time.time()) + 3600)

    store.save("protect.invalid", 443, "someone", token)

    assert store.load("protect.invalid", 443, "someone") == token


def test_expired_token_is_not_returned(tmp_path: Any) -> None:
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.save("protect.invalid", 443, "someone", make_token(int(time.time()) - 10))

    assert store.load("protect.invalid", 443, "someone") is None


def test_token_expiring_within_the_margin_is_not_returned(tmp_path: Any) -> None:
    """A token about to expire would strand a download that has already started."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.save(
        "protect.invalid", 443, "someone", make_token(int(time.time()) + EXPIRY_MARGIN_SECONDS - 5)
    )

    assert store.load("protect.invalid", 443, "someone") is None


def test_tokens_are_scoped_to_host_and_user(tmp_path: Any) -> None:
    store = SessionStore(str(tmp_path / "sessions.json"))
    token = make_token(int(time.time()) + 3600)
    store.save("protect.invalid", 443, "someone", token)

    assert store.load("other.invalid", 443, "someone") is None
    assert store.load("protect.invalid", 443, "someone-else") is None
    assert store.load("protect.invalid", 7443, "someone") is None


def test_clear_removes_a_rejected_token(tmp_path: Any) -> None:
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.save("protect.invalid", 443, "someone", make_token(int(time.time()) + 3600))

    store.clear("protect.invalid", 443, "someone")

    assert store.load("protect.invalid", 443, "someone") is None


def test_a_corrupt_store_is_ignored_rather_than_fatal(tmp_path: Any) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("{ this is not json")
    store = SessionStore(str(path))

    assert store.load("protect.invalid", 443, "someone") is None

    # It must still be usable afterwards, overwriting the damaged file.
    token = make_token(int(time.time()) + 3600)
    store.save("protect.invalid", 443, "someone", token)
    assert store.load("protect.invalid", 443, "someone") == token


def test_token_without_expiry_gets_a_conservative_lifetime(tmp_path: Any) -> None:
    store = SessionStore(str(tmp_path / "sessions.json"))

    store.save("protect.invalid", 443, "someone", "opaque-token-with-no-claims")

    # Still usable now, because an assumed lifetime was applied rather than nothing.
    assert store.load("protect.invalid", 443, "someone") == "opaque-token-with-no-claims"
