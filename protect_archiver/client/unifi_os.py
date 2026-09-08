import logging
import os
import sys

from typing import Any
from typing import Dict
from typing import Optional

import requests

from protect_archiver.errors import ProtectError
from protect_archiver.session_store import SessionStore


# UniFi OS answers a credentials-only login with this non-standard status when the
# account is backed by Ubiquiti SSO and a second factor is enrolled.
MFA_REQUIRED_STATUS = 499
MFA_REQUIRED_CODE = "MFA_AUTH_REQUIRED"

# The UBIC_2FA challenge cookie handed back with the MFA_AUTH_REQUIRED response is
# short-lived (observed: 10 minutes), so the code has to be supplied promptly.
MFA_COOKIE_NAME = "UBIC_2FA"


class UniFiOSClient:
    def __init__(
        self,
        protocol: str,
        address: str,
        port: int,
        username: str,
        password: str,
        verify_ssl: bool,
        mfa_code: Optional[str] = None,
        session_store: Optional[SessionStore] = None,
        use_session_store: bool = True,
    ) -> None:
        self.protocol = protocol
        self.address = address
        self.port = port
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl

        # A supplied code is single-use: it is consumed by the first login that needs it
        # so a later re-authentication cannot silently replay a stale code.
        self.mfa_code = mfa_code
        self.use_session_store = use_session_store
        self.session_store = session_store or SessionStore()

        self._access_key: Optional[str] = None
        self._api_token: Optional[str] = None

        self.authority = f"{self.protocol}://{self.address}:{self.port}"
        self.base_path = "/proxy/protect/api"

    @property
    def _auth_uri(self) -> str:
        return f"{self.protocol}://{self.address}:{self.port}/api/auth/login"

    def _login(self, payload: Dict[str, Any], cookies: Optional[Dict[str, str]] = None) -> Any:
        return requests.post(
            self._auth_uri,
            json=payload,
            cookies=cookies or {},
            verify=self.verify_ssl,
            timeout=30,
        )

    def _consume_mfa_code(self) -> Optional[str]:
        """Return a one-time code from configuration, clearing it so it is used once."""
        code = self.mfa_code
        self.mfa_code = None
        return code

    @staticmethod
    def _describe_authenticators(response_data: Dict[str, Any]) -> str:
        """Summarise how the second factor will reach the operator."""
        authenticators = response_data.get("data", {}).get("authenticators") or []
        described = []
        for item in authenticators:
            kind = str(item.get("type"))
            email = item.get("email")
            described.append(f"{kind} to {email}" if email else kind)
        return ", ".join(described)

    def _obtain_mfa_code(self, response_data: Dict[str, Any]) -> str:
        """Get the second-factor code, from configuration or by asking the operator."""
        code = self._consume_mfa_code() or os.environ.get("PROTECT_MFA_CODE") or ""
        if code.strip():
            return code.strip()

        delivery = self._describe_authenticators(response_data)

        if not sys.stdin.isatty():
            # Prompting here would hang a scheduled or piped run forever, which is a far
            # worse failure than stopping with an explanation.
            logging.error(
                f"User {self.username} requires a multi-factor code"
                f" ({delivery or 'unknown method'}) but there is no terminal to ask on."
                " Re-run interactively, or supply the code with --mfa-code or"
                " PROTECT_MFA_CODE."
            )
            raise ProtectError(2)

        logging.info(f"A multi-factor code has been sent: {delivery or 'check your authenticator'}")
        try:
            return input("Enter the UniFi multi-factor code: ").strip()
        except EOFError:
            logging.error("No multi-factor code was entered")
            raise ProtectError(2)

    @staticmethod
    def _extract_mfa_cookie(response: Any, response_data: Dict[str, Any]) -> Optional[str]:
        """Pull the UBIC_2FA challenge cookie from wherever this firmware put it."""
        cookie = response.cookies.get(MFA_COOKIE_NAME)
        if cookie:
            return str(cookie)

        # Some firmware returns it only in the body, as a raw 'UBIC_2FA=<jwt>' string.
        raw = response_data.get("data", {}).get("mfaCookie")
        if isinstance(raw, str) and raw.startswith(f"{MFA_COOKIE_NAME}="):
            return raw.split("=", 1)[1].split(";", 1)[0]
        return None

    @staticmethod
    def _json_or_empty(response: Any) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _token_from_response(response: Any) -> Optional[str]:
        return response.cookies.get("TOKEN") or response.cookies.get("UOS_TOKEN")

    def fetch_session_cookie_token(self) -> str:
        """Authenticate and return the UniFi OS session cookie token.

        Handles the two-step SSO multi-factor exchange: a credentials-only POST is
        answered with MFA_AUTH_REQUIRED plus a short-lived challenge cookie, and the
        code is then submitted alongside that cookie.
        """
        # 'rememberMe' asks UniFi OS for a longer-lived session, which directly reduces
        # how often an emailed code has to be typed.
        credentials: Dict[str, Any] = {
            "username": self.username,
            "password": self.password,
            "rememberMe": True,
        }

        response = self._login(credentials)
        data = self._json_or_empty(response)

        if response.status_code == MFA_REQUIRED_STATUS or data.get("code") == MFA_REQUIRED_CODE:
            mfa_cookie = self._extract_mfa_cookie(response, data)
            if not mfa_cookie:
                logging.error(
                    "UniFi OS asked for a multi-factor code but returned no"
                    f" {MFA_COOKIE_NAME} challenge cookie, so the code cannot be submitted"
                )
                raise ProtectError(2)

            code = self._obtain_mfa_code(data)
            if not code:
                logging.error("No multi-factor code was provided")
                raise ProtectError(2)

            response = self._login(
                {**credentials, "token": code}, cookies={MFA_COOKIE_NAME: mfa_cookie}
            )
            data = self._json_or_empty(response)

        if response.status_code == 429:
            logging.error(
                "UniFi OS is rate limiting authentication attempts. Wait a few minutes"
                " before retrying; the archiver caches its session token to avoid this."
            )
            raise ProtectError(2)

        if response.status_code != 200:
            message = data.get("message") or data.get("error") or "(no information available)"
            logging.error(
                f"Authentication failed with status code {response.status_code}: {message}"
            )
            raise ProtectError(2)

        session_cookie_token = self._token_from_response(response)
        if not session_cookie_token:
            logging.error("Authentication succeeded but no session cookie was returned")
            raise ProtectError(2)

        logging.info(f"Successfully authenticated as user {self.username}")

        if self.use_session_store:
            self.session_store.save(self.address, self.port, self.username, session_cookie_token)

        return str(session_cookie_token)

    def get_api_token(self, force: bool = False) -> str:
        if force:
            self._api_token = None
            if self.use_session_store:
                # The caller only forces after the server rejected the token, so the
                # cached copy is known bad and must not be handed out again.
                self.session_store.clear(self.address, self.port, self.username)

        if self._api_token is None and self.use_session_store:
            self._api_token = self.session_store.load(self.address, self.port, self.username)

        if self._api_token is None:
            self._api_token = self.fetch_session_cookie_token()

        return self._api_token
