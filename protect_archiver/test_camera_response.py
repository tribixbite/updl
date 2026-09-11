"""Regression tests for camera-list responses from UniFi OS consoles."""

import logging

from datetime import datetime
from typing import Any
from typing import Dict

import pytest
import requests

from protect_archiver.client.legacy import LegacyClient
from protect_archiver.client.unifi_os import UniFiOSClient
from protect_archiver.downloader.get_camera_list import get_camera_list
from protect_archiver.errors import ProtectError

CAMERAS_URL = "https://protect.invalid:443/proxy/protect/api/cameras"
AUTH_URL = "https://protect.invalid:443/api/auth/login"
LEGACY_CAMERAS_URL = "https://legacy.invalid:7443/api/cameras"


def make_client() -> UniFiOSClient:
    client = UniFiOSClient(
        protocol="https",
        address="protect.invalid",
        port=443,
        username="someone",
        password="secret",
        verify_ssl=False,
        use_session_store=False,
    )
    client._api_token = "stale-token"
    return client


def camera_json(recording_start: int = 1_700_000_000_123) -> Dict[str, Any]:
    return {
        "id": "camera-0001",
        "name": "Front Door",
        "stats": {"video": {"recordingStart": recording_start}},
    }


def add_token_refresh(responses: Any, token: str = "fresh-token") -> None:
    responses.add(
        responses.POST,
        AUTH_URL,
        headers={"Set-Cookie": f"TOKEN={token}"},
        json={},
    )


def assert_code_three(error: Any) -> None:
    assert error.value.code == 3


def test_camera_request_has_bounded_safe_http_options(monkeypatch: Any) -> None:
    captured: Dict[str, Any] = {}

    def fake_get(uri: str, **options: Any) -> requests.Response:
        captured["uri"] = uri
        captured.update(options)
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = b"[]"
        return response

    monkeypatch.setattr(requests, "get", fake_get)

    assert get_camera_list(make_client()) == []

    assert captured["uri"] == CAMERAS_URL
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["timeout"] == 30
    assert captured["allow_redirects"] is False


def test_valid_camera_response_preserves_naive_local_recording_time(responses: Any) -> None:
    recording_start = 1_700_000_000_123
    responses.add(responses.GET, CAMERAS_URL, json=[camera_json(recording_start)])

    cameras = get_camera_list(make_client())

    assert len(cameras) == 1
    assert cameras[0].id == "camera-0001"
    assert cameras[0].name == "Front Door"
    assert cameras[0].recording_start == datetime.fromtimestamp(recording_start / 1000)
    assert cameras[0].recording_start.tzinfo is None


def test_empty_camera_list_is_a_valid_response(responses: Any) -> None:
    responses.add(responses.GET, CAMERAS_URL, json=[])

    assert get_camera_list(make_client()) == []


def test_legacy_client_keeps_bearer_authentication(responses: Any) -> None:
    client = LegacyClient(
        protocol="https",
        address="legacy.invalid",
        port=7443,
        username="someone",
        password="secret",
        verify_ssl=False,
    )
    client._api_token = "legacy-token"
    responses.add(responses.GET, LEGACY_CAMERAS_URL, json=[])

    assert get_camera_list(client) == []
    assert responses.calls[0].request.headers["Authorization"] == "Bearer legacy-token"


@pytest.mark.parametrize("status", [403, 500])
def test_non_auth_http_errors_raise_without_reporting_success(
    responses: Any, caplog: Any, status: int
) -> None:
    responses.add(responses.GET, CAMERAS_URL, status=status, json={"error": "failed"})

    with caplog.at_level(logging.INFO), pytest.raises(ProtectError) as error:
        get_camera_list(make_client())

    assert_code_three(error)
    assert len(responses.calls) == 1
    assert "Successfully retrieved" not in caplog.text


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        ("", "application/json"),
        ("this is not json", "application/json"),
        ("{}", "application/json"),
        ('{"id": "camera-0001"}', "application/json"),
    ],
)
def test_invalid_or_non_list_json_raises_without_reporting_success(
    responses: Any, caplog: Any, body: str, content_type: str
) -> None:
    responses.add(
        responses.GET,
        CAMERAS_URL,
        body=body,
        headers={"Content-Type": content_type},
    )

    with caplog.at_level(logging.INFO), pytest.raises(ProtectError) as error:
        get_camera_list(make_client())

    assert_code_three(error)
    assert "Successfully retrieved" not in caplog.text


@pytest.mark.parametrize(
    "camera",
    [
        {"name": "Front Door", "stats": {"video": {"recordingStart": 1}}},
        {"id": "camera-0001", "stats": {"video": {"recordingStart": 1}}},
        {"id": "camera-0001", "name": "Front Door"},
        {"id": "camera-0001", "name": "Front Door", "stats": {}},
        {
            "id": "camera-0001",
            "name": "Front Door",
            "stats": {"video": {"recordingStart": "yesterday"}},
        },
    ],
)
def test_malformed_camera_entries_raise_protect_error(
    responses: Any, caplog: Any, camera: Dict[str, Any]
) -> None:
    responses.add(responses.GET, CAMERAS_URL, json=[camera])

    with caplog.at_level(logging.INFO), pytest.raises(ProtectError) as error:
        get_camera_list(make_client())

    assert_code_three(error)
    assert "Successfully retrieved" not in caplog.text


def test_unauthorized_response_refreshes_token_once(responses: Any) -> None:
    responses.add(responses.GET, CAMERAS_URL, status=401, json={"error": "unauthorized"})
    add_token_refresh(responses)
    responses.add(responses.GET, CAMERAS_URL, json=[camera_json()])

    cameras = get_camera_list(make_client())

    assert [camera.id for camera in cameras] == ["camera-0001"]
    assert len(responses.calls) == 3
    assert "TOKEN=stale-token" in responses.calls[0].request.headers["Cookie"]
    assert "TOKEN=fresh-token" in responses.calls[2].request.headers["Cookie"]


@pytest.mark.parametrize("login_path", ["/login", "/signin"])
def test_same_origin_login_redirect_refreshes_without_following_redirect(
    responses: Any, login_path: str
) -> None:
    responses.add(
        responses.GET,
        CAMERAS_URL,
        status=302,
        headers={"Location": f"https://protect.invalid:443{login_path}?return=/protect"},
    )
    add_token_refresh(responses)
    responses.add(responses.GET, CAMERAS_URL, json=[camera_json()])

    cameras = get_camera_list(make_client())

    assert [camera.id for camera in cameras] == ["camera-0001"]
    assert [call.request.url for call in responses.calls] == [CAMERAS_URL, AUTH_URL, CAMERAS_URL]


def test_cross_origin_redirect_is_not_followed_or_treated_as_auth(responses: Any) -> None:
    responses.add(
        responses.GET,
        CAMERAS_URL,
        status=302,
        headers={"Location": "https://attacker.invalid/login"},
    )

    with pytest.raises(ProtectError) as error:
        get_camera_list(make_client())

    assert_code_three(error)
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == CAMERAS_URL


def test_camera_request_timeout_raises_protect_error(responses: Any, caplog: Any) -> None:
    responses.add(responses.GET, CAMERAS_URL, body=requests.Timeout("camera request timed out"))

    with caplog.at_level(logging.INFO), pytest.raises(ProtectError) as error:
        get_camera_list(make_client())

    assert_code_three(error)
    assert len(responses.calls) == 1
    assert "Successfully retrieved" not in caplog.text


def test_timeout_while_refreshing_login_raises_protect_error(responses: Any) -> None:
    responses.add(responses.GET, CAMERAS_URL, status=401, json={"error": "unauthorized"})
    responses.add(responses.POST, AUTH_URL, body=requests.Timeout("login request timed out"))

    with pytest.raises(ProtectError) as error:
        get_camera_list(make_client())

    assert_code_three(error)
    assert len(responses.calls) == 2


def test_html_login_response_refreshes_token_once(responses: Any) -> None:
    responses.add(
        responses.GET,
        CAMERAS_URL,
        body="<html><title>Sign in</title></html>",
        headers={"Content-Type": "text/html; charset=utf-8"},
    )
    add_token_refresh(responses)
    responses.add(responses.GET, CAMERAS_URL, json=[camera_json()])

    cameras = get_camera_list(make_client())

    assert [camera.id for camera in cameras] == ["camera-0001"]
    assert len(responses.calls) == 3


@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        (401, '{"error":"unauthorized"}', {"Content-Type": "application/json"}),
        (200, "<html>private-login-page</html>", {"Content-Type": "text/html"}),
        (302, "", {"Location": "/login"}),
    ],
)
def test_auth_like_response_retries_only_once_and_does_not_log_response_body(
    responses: Any,
    caplog: Any,
    status: int,
    body: str,
    headers: Dict[str, str],
) -> None:
    responses.add(responses.GET, CAMERAS_URL, status=status, body=body, headers=headers)
    add_token_refresh(responses)
    responses.add(responses.GET, CAMERAS_URL, status=status, body=body, headers=headers)

    with caplog.at_level(logging.INFO), pytest.raises(ProtectError) as error:
        get_camera_list(make_client())

    assert_code_three(error)
    assert len(responses.calls) == 3
    assert sum(call.request.method == "POST" for call in responses.calls) == 1
    assert "private-login-page" not in caplog.text
    assert "Successfully retrieved" not in caplog.text


def test_camera_request_sends_both_token_cookies_and_bearer_header(responses: Any) -> None:
    responses.add(responses.GET, CAMERAS_URL, json=[camera_json()])

    client = make_client()
    get_camera_list(client)

    cookie_header = responses.calls[0].request.headers.get("Cookie", "")
    assert "TOKEN=stale-token" in cookie_header
    assert "UOS_TOKEN=stale-token" in cookie_header
    assert responses.calls[0].request.headers.get("Authorization") == "Bearer stale-token"


def test_default_unifi_hostname_logs_guidance_on_failure(responses: Any, caplog: Any) -> None:
    unifi_url = "https://unifi:443/proxy/protect/api/cameras"
    responses.add(
        responses.GET,
        unifi_url,
        status=200,
        body="<html>gateway</html>",
        headers={"Content-Type": "text/html"},
    )
    responses.add(
        responses.POST,
        "https://unifi:443/api/auth/login",
        headers={"Set-Cookie": "TOKEN=tok"},
        json={},
    )
    responses.add(
        responses.GET,
        unifi_url,
        status=200,
        body="<html>gateway</html>",
        headers={"Content-Type": "text/html"},
    )

    client = UniFiOSClient(
        protocol="https",
        address="unifi",
        port=443,
        username="someone",
        password="secret",
        verify_ssl=False,
        use_session_store=False,
    )
    client._api_token = "stale"

    with caplog.at_level(logging.INFO), pytest.raises(ProtectError) as error:
        get_camera_list(client)

    assert_code_three(error)
    assert "Note: 'unifi' is the default hostname" in caplog.text
