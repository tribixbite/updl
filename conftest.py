import json
import os
import shutil

from datetime import datetime
from typing import Any
from typing import Dict

import pytest
import responses as responses_

from protect_archiver.client import ProtectClient
from protect_archiver.dataclasses import Camera


@pytest.fixture(autouse=True)
def isolated_session_store(tmp_path: Any, monkeypatch: Any) -> None:
    """Keep the suite away from the real per-user session store.

    Authenticating in a test would otherwise write a token into the developer's own
    profile directory, and a stale entry there could silently satisfy a later test.
    """
    monkeypatch.setenv("PROTECT_SESSION_STORE", str(tmp_path / "sessions.json"))


@pytest.fixture
def sample_bootstrap_json() -> Dict[str, Any]:
    with open("fixtures/bootstrap.json") as fp:
        return json.load(fp)


@pytest.fixture
def sample_token_json() -> Dict[str, Any]:
    with open("fixtures/token.json") as fp:
        return json.load(fp)


@pytest.fixture
def sample_access_key_json() -> Dict[str, Any]:
    with open("fixtures/access-key.json") as fp:
        return json.load(fp)


@pytest.fixture
def sample_camera() -> Camera:
    return Camera(
        id="exteriorCameraId",
        name="Exterior",
        recording_start=datetime(2020, 1, 8, 23, 26, 9, 586000),
    )


@pytest.fixture
def test_output_dest() -> Any:
    test_output_dest = os.path.join(os.path.dirname(__file__), "test_output")
    os.makedirs(test_output_dest)
    yield test_output_dest
    shutil.rmtree(test_output_dest, ignore_errors=False, onerror=None)


@pytest.fixture
def client(test_output_dest: str) -> Any:
    return ProtectClient(destination_path=test_output_dest, password="test", download_timeout=0.01)


@pytest.fixture
def responses() -> Any:
    # XXX(dcramer): pytest-responses doesnt let you change this behavior yet
    with responses_.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps
