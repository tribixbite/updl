"""Fetch camera metadata without mistaking a console login page for API data."""

import logging

from typing import Any
from typing import Dict
from typing import List
from urllib.parse import urljoin
from urllib.parse import urlsplit

import requests

from protect_archiver.client.unifi_os import UniFiOSClient
from protect_archiver.errors import ProtectError


def _looks_like_login(response: requests.Response, uri: str) -> bool:
    if response.status_code == 401:
        return True
    if response.status_code == 200:
        return "text/html" in response.headers.get(
            "Content-Type", ""
        ).lower() or response.content.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    if response.is_redirect:
        target = urlsplit(urljoin(uri, response.headers.get("Location", "")))
        source = urlsplit(uri)
        return (target.scheme, target.netloc) == (source.scheme, source.netloc) and (
            target.path.rstrip("/") in ("/login", "/signin")
        )
    return False


def fetch_camera_data(session: Any) -> List[Dict[str, Any]]:
    uri = f"{session.authority}{session.base_path}/cameras"
    for attempt in range(2):
        options: Dict[str, Any] = {
            "headers": {"Accept": "application/json"},
            "verify": session.verify_ssl,
            "timeout": 30,
            # A redirected console page can return HTTP 200 without being API data.
            "allow_redirects": False,
        }
        try:
            token = session.get_api_token(force=True) if attempt else session.get_api_token()
            if isinstance(session, UniFiOSClient):
                options["cookies"] = {"TOKEN": token, "UOS_TOKEN": token}
                options["headers"]["Authorization"] = f"Bearer {token}"
            else:
                options["headers"]["Authorization"] = f"Bearer {token}"
            response = requests.get(uri, **options)
        except requests.RequestException as exc:
            logging.error("Could not load camera list from %s (%s)", uri, type(exc).__name__)
            raise ProtectError(3) from None

        with response:
            if attempt == 0 and _looks_like_login(response, uri):
                logging.warning(
                    "Camera API returned an authentication error, login redirect, or HTML;"
                    " refreshing the session once"
                )
                continue

            detail = f"HTTP {response.status_code}"
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    detail += ", empty or non-JSON response"
                else:
                    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
                        return data
                    detail += ", expected a JSON array of cameras"

            # Never include response bodies or redirect URLs: they can contain secrets.
            logging.error(
                "Could not load camera list from %s: %s. Check the console address/port,"
                " that Protect is running, and that the account has Protect camera access.",
                uri,
                detail,
            )
            if getattr(session, "address", None) == "unifi":
                logging.error(
                    "Note: 'unifi' is the default hostname. If your Protect console is at an IP"
                    " address (e.g. 192.168.1.1), pass it using -a/--address or set the"
                    " PROTECT_ADDRESS environment variable."
                )
            raise ProtectError(3)
    raise ProtectError(3)  # pragma: no cover
