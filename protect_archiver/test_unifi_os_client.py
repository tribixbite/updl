"""Tests for the UniFi OS login exchange.

The multi-factor payloads here are the shapes a UDM Pro SE actually returned, so that a
firmware change which alters them shows up as a test failure rather than as a run that
stops asking for a code and silently fails to authenticate.
"""

import base64
import json
import time

from typing import Any
from typing import Dict

import pytest

from protect_archiver.client.unifi_os import MFA_COOKIE_NAME
from protect_archiver.client.unifi_os import MFA_REQUIRED_STATUS
from protect_archiver.client.unifi_os import UniFiOSClient
from protect_archiver.errors import ProtectError
from protect_archiver.session_store import SessionStore


AUTH_URL = "https://protect.invalid:443/api/auth/login"


def make_token(seconds_ahead: int = 3600) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + seconds_ahead}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{payload}.signature"


def mfa_challenge_body(with_cookie_in_body: bool = True) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "message": "MFA token required to authenticate to SSO",
        "code": "MFA_AUTH_REQUIRED",
        "data": {
            "required": "2fa",
            "authenticators": [
                {
                    "id": "00000000-0000-4000-8000-000000000002",
                    "type": "email",
                    "email": "someone@example.com",
                    "status": "active",
                }
            ],
            "user": {"id": "00000000-0000-4000-8000-000000000001"},
        },
    }
    if with_cookie_in_body:
        body["data"]["mfaCookie"] = f"{MFA_COOKIE_NAME}=challenge-jwt; Path=/; HttpOnly"
    return body


def make_client(tmp_path: Any, **kwargs: Any) -> UniFiOSClient:
    defaults = dict(
        protocol="https",
        address="protect.invalid",
        port=443,
        username="someone",
        password="secret",
        verify_ssl=False,
        session_store=SessionStore(str(tmp_path / "sessions.json")),
    )
    defaults.update(kwargs)
    return UniFiOSClient(**defaults)  # type: ignore[arg-type]


def test_plain_login_returns_and_caches_the_token(responses: Any, tmp_path: Any) -> None:
    token = make_token()
    responses.add(responses.POST, AUTH_URL, headers={"Set-Cookie": f"TOKEN={token}"}, json={})
    client = make_client(tmp_path)

    assert client.get_api_token() == token
    # A second call must not hit the network again.
    assert client.get_api_token() == token
    assert len(responses.calls) == 1


def test_supplied_code_completes_the_two_step_exchange(responses: Any, tmp_path: Any) -> None:
    token = make_token()
    responses.add(responses.POST, AUTH_URL, status=MFA_REQUIRED_STATUS, json=mfa_challenge_body())
    responses.add(responses.POST, AUTH_URL, headers={"Set-Cookie": f"TOKEN={token}"}, json={})

    client = make_client(tmp_path, mfa_code="123456")

    assert client.get_api_token() == token
    assert len(responses.calls) == 2

    second_request = responses.calls[1].request
    submitted = json.loads(second_request.body)
    # The code goes in a 'token' field alongside the original credentials...
    assert submitted["token"] == "123456"
    assert submitted["username"] == "someone"
    # ...and the short-lived challenge cookie has to come back with it.
    assert "challenge-jwt" in second_request.headers["Cookie"]


def test_challenge_cookie_is_read_from_the_set_cookie_header(responses: Any, tmp_path: Any) -> None:
    """Some firmware returns UBIC_2FA as a real cookie rather than in the body."""
    token = make_token()
    responses.add(
        responses.POST,
        AUTH_URL,
        status=MFA_REQUIRED_STATUS,
        headers={"Set-Cookie": f"{MFA_COOKIE_NAME}=cookie-jwt"},
        json=mfa_challenge_body(with_cookie_in_body=False),
    )
    responses.add(responses.POST, AUTH_URL, headers={"Set-Cookie": f"TOKEN={token}"}, json={})

    client = make_client(tmp_path, mfa_code="123456")

    assert client.get_api_token() == token
    assert "cookie-jwt" in responses.calls[1].request.headers["Cookie"]


def test_code_is_used_once_and_not_replayed(responses: Any, tmp_path: Any) -> None:
    """A one-time code must not be resubmitted on a later re-authentication."""
    responses.add(responses.POST, AUTH_URL, status=MFA_REQUIRED_STATUS, json=mfa_challenge_body())
    responses.add(
        responses.POST, AUTH_URL, headers={"Set-Cookie": f"TOKEN={make_token()}"}, json={}
    )
    client = make_client(tmp_path, mfa_code="123456")
    client.get_api_token()

    assert client.mfa_code is None


def test_missing_code_without_a_terminal_fails_instead_of_hanging(
    responses: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    responses.add(responses.POST, AUTH_URL, status=MFA_REQUIRED_STATUS, json=mfa_challenge_body())
    monkeypatch.delenv("PROTECT_MFA_CODE", raising=False)
    # Nothing should ever read from stdin in this situation.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input", lambda *args: pytest.fail("must not prompt without a terminal")
    )

    client = make_client(tmp_path)

    with pytest.raises(ProtectError):
        client.get_api_token()


def test_code_can_come_from_the_environment(
    responses: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    token = make_token()
    responses.add(responses.POST, AUTH_URL, status=MFA_REQUIRED_STATUS, json=mfa_challenge_body())
    responses.add(responses.POST, AUTH_URL, headers={"Set-Cookie": f"TOKEN={token}"}, json={})
    monkeypatch.setenv("PROTECT_MFA_CODE", "654321")

    client = make_client(tmp_path)

    assert client.get_api_token() == token
    assert json.loads(responses.calls[1].request.body)["token"] == "654321"


def test_rate_limiting_is_reported_clearly(responses: Any, tmp_path: Any) -> None:
    responses.add(responses.POST, AUTH_URL, status=429, json={"message": "too many requests"})
    client = make_client(tmp_path)

    with pytest.raises(ProtectError):
        client.get_api_token()


def test_wrong_code_does_not_yield_a_token(responses: Any, tmp_path: Any) -> None:
    responses.add(responses.POST, AUTH_URL, status=MFA_REQUIRED_STATUS, json=mfa_challenge_body())
    responses.add(responses.POST, AUTH_URL, status=401, json={"message": "invalid token"})
    client = make_client(tmp_path, mfa_code="000000")

    with pytest.raises(ProtectError):
        client.get_api_token()


def test_stored_token_is_reused_without_authenticating(tmp_path: Any) -> None:
    """The whole point of the store: a later run must not need another emailed code."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    token = make_token()
    store.save("protect.invalid", 443, "someone", token)

    client = make_client(tmp_path, session_store=store)

    # No responses are registered, so any HTTP call would raise.
    assert client.get_api_token() == token


def test_forcing_a_refresh_discards_the_stored_token(responses: Any, tmp_path: Any) -> None:
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.save("protect.invalid", 443, "someone", make_token())
    fresh = make_token(7200)
    responses.add(responses.POST, AUTH_URL, headers={"Set-Cookie": f"TOKEN={fresh}"}, json={})

    client = make_client(tmp_path, session_store=store)

    assert client.get_api_token(force=True) == fresh
    assert store.load("protect.invalid", 443, "someone") == fresh


def test_session_store_can_be_disabled(responses: Any, tmp_path: Any) -> None:
    store = SessionStore(str(tmp_path / "sessions.json"))
    responses.add(
        responses.POST, AUTH_URL, headers={"Set-Cookie": f"TOKEN={make_token()}"}, json={}
    )

    client = make_client(tmp_path, session_store=store, use_session_store=False)
    client.get_api_token()

    assert store.load("protect.invalid", 443, "someone") is None
