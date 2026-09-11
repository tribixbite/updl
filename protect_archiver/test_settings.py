import json
import os
import sys

from typing import Any

import pytest

from click.testing import CliRunner

from protect_archiver import settings
from protect_archiver.cli import main
from protect_archiver.cli import resolve_argv
from protect_archiver.errors import ProtectError


def use_config(tmp_path: Any, monkeypatch: Any) -> str:
    path = str(tmp_path / "config.json")
    monkeypatch.setenv("UPDL_CONFIG", path)
    return path


def test_nothing_remembered_yet(tmp_path: Any, monkeypatch: Any) -> None:
    use_config(tmp_path, monkeypatch)

    assert settings.load() == {}
    assert settings.as_default_map() == {}


def test_round_trips_the_identifying_options(tmp_path: Any, monkeypatch: Any) -> None:
    path = use_config(tmp_path, monkeypatch)

    settings.save({"address": "nvr.invalid", "username": "archiver", "dest": "/srv/protect"})

    assert json.load(open(path)) == {
        "address": "nvr.invalid",
        "username": "archiver",
        "dest": "/srv/protect",
    }


def test_the_password_is_never_written(tmp_path: Any, monkeypatch: Any) -> None:
    """The whole point of the design: credentials do not go in the config file."""
    path = use_config(tmp_path, monkeypatch)

    settings.save(
        {
            "address": "nvr.invalid",
            "username": "archiver",
            "password": "hunter2",
            "mfa_code": "123456",
        }
    )

    contents = open(path).read()
    assert "hunter2" not in contents
    assert "123456" not in contents
    assert "password" not in contents
    assert "mfa" not in contents


def test_transient_flags_are_not_remembered(tmp_path: Any, monkeypatch: Any) -> None:
    """Silently repeating --ignore-state on a later run would re-download everything."""
    path = use_config(tmp_path, monkeypatch)

    settings.save({"address": "nvr.invalid", "ignore_state": True, "reconcile": True})

    stored = json.load(open(path))
    assert "ignore_state" not in stored
    assert "reconcile" not in stored


def test_default_map_uses_click_parameter_names(tmp_path: Any, monkeypatch: Any) -> None:
    """Click matches a default_map by parameter name, so 'dest' must become 'dest_option'."""
    use_config(tmp_path, monkeypatch)
    settings.save({"address": "nvr.invalid", "dest": "/srv/protect"})

    default_map = settings.as_default_map()

    assert default_map["sync"]["dest_option"] == "/srv/protect"
    assert default_map["sync"]["address"] == "nvr.invalid"
    assert "dest" not in default_map["sync"]


def test_verify_inherits_only_the_destination(tmp_path: Any, monkeypatch: Any) -> None:
    """verify's --level choices exclude 'none', so it must not inherit sync's."""
    use_config(tmp_path, monkeypatch)
    settings.save({"dest": "/srv/protect", "verify_level": "none", "address": "nvr.invalid"})

    verify_defaults = settings.as_default_map()["verify"]

    assert verify_defaults == {"dest_option": "/srv/protect"}


def test_empty_values_are_dropped(tmp_path: Any, monkeypatch: Any) -> None:
    path = use_config(tmp_path, monkeypatch)

    settings.save({"address": "nvr.invalid", "username": "", "dest": None})

    assert json.load(open(path)) == {"address": "nvr.invalid"}


def test_a_corrupt_config_is_ignored_not_fatal(tmp_path: Any, monkeypatch: Any) -> None:
    path = use_config(tmp_path, monkeypatch)
    with open(path, "w") as fp:
        fp.write("{ not json")

    assert settings.load() == {}

    settings.save({"address": "nvr.invalid"})
    assert settings.load() == {"address": "nvr.invalid"}


def test_config_file_is_owner_only(tmp_path: Any, monkeypatch: Any) -> None:
    path = use_config(tmp_path, monkeypatch)
    settings.save({"address": "nvr.invalid"})

    if os.name != "nt":
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    else:
        assert os.path.isfile(path)


def test_sync_is_the_primary_command() -> None:
    commands = {"sync", "verify", "download", "events"}

    assert resolve_argv([], commands) == ["sync"]
    assert resolve_argv([r"C:\unifi"], commands) == ["sync", r"C:\unifi"]
    # Options with no sub-command are a sync too, which is what makes `updl -d X` work.
    assert resolve_argv(["-d", "/srv"], commands) == ["sync", "-d", "/srv"]
    # Explicit commands remain available for backwards compatibility.
    assert resolve_argv(["sync", "/srv"], commands) == ["sync", "/srv"]
    assert resolve_argv(["verify", "/srv"], commands) == ["verify", "/srv"]
    # Top-level help must not become `sync --help`.
    assert resolve_argv(["--help"], commands) == ["--help"]


def test_top_level_help_presents_destination_as_primary_usage() -> None:
    from protect_archiver.cli.base import cli

    result = CliRunner().invoke(cli, ["--help"], prog_name="updl")

    assert result.exit_code == 0
    assert "Usage: updl [OPTIONS] [DEST]" in result.output
    assert "updl DEST --help" in " ".join(result.output.split())
    assert "--address" in result.output
    assert "--username" in result.output


def test_entrypoint_routes_a_destination_to_sync(tmp_path: Any, monkeypatch: Any) -> None:
    from protect_archiver.cli.base import cli

    use_config(tmp_path, monkeypatch)
    monkeypatch.setenv("PROTECT_USERNAME", "archiver")
    monkeypatch.setenv("PROTECT_PASSWORD", "secret")

    received = {}

    def record_sync(**kwargs: Any) -> None:
        received.update(kwargs)

    monkeypatch.setattr(cli.commands["sync"], "callback", record_sync)
    monkeypatch.setattr(sys, "argv", ["updl", str(tmp_path)])

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 0
    assert received["dest_argument"] == str(tmp_path)


def test_entrypoint_converts_protect_error_to_exit_code(monkeypatch: Any) -> None:
    from protect_archiver.cli.base import cli

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ProtectError(3)

    monkeypatch.setattr(cli, "main", fail)
    monkeypatch.setattr(sys, "argv", ["updl", r"C:\unifi"])

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 3
