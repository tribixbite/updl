from datetime import datetime
from typing import Any

import pytest

from click.testing import CliRunner

from protect_archiver.cli.base import cli
from protect_archiver.cli.sync import select_cameras
from protect_archiver.dataclasses import Camera
from protect_archiver.errors import ProtectError


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("UPDL_CONFIG", str(tmp_path / "config.json"))


@pytest.fixture
def cameras() -> list[Camera]:
    return [
        Camera(id="front-id", name="Front", recording_start=datetime.min),
        Camera(id="rear-id", name="Rear", recording_start=datetime.min),
    ]


def test_camera_selection_trims_ids_and_preserves_discovery_order(cameras: list[Camera]) -> None:
    selected = select_cameras(cameras, " rear-id, front-id ")

    assert [camera.id for camera in selected] == ["front-id", "rear-id"]


def test_all_camera_selection_preserves_the_discovered_list(cameras: list[Camera]) -> None:
    assert select_cameras(cameras, "all") is cameras


@pytest.mark.parametrize("selection", ["", " ", ",", " , "])
def test_empty_camera_selection_is_rejected(cameras: list[Camera], selection: str) -> None:
    with pytest.raises(ProtectError) as error:
        select_cameras(cameras, selection)

    assert error.value.code == 1


def test_any_unknown_camera_id_rejects_the_whole_selection(cameras: list[Camera]) -> None:
    with pytest.raises(ProtectError) as error:
        select_cameras(cameras, "front-id, missing-id")

    assert error.value.code == 1


def test_short_help_alias_shows_top_level_help() -> None:
    result = CliRunner().invoke(cli, ["-h"], prog_name="updl")

    assert result.exit_code == 0
    assert "Usage: updl [OPTIONS] [DEST]" in result.output
    assert "Commands:" in result.output
