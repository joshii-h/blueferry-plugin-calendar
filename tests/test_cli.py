"""The command line: setup, calendars, activation files."""
from __future__ import annotations

import argparse
import io

import blueferry.plugin_api as api
import pytest
from blueferry.plugin_api.manifest import ManifestError
from fakes import PASSWORD, USER, FakeSecret, FakeServer

from blueferry_calendar import PLUGIN_ID, manifest_text
from blueferry_calendar import __main__ as cli
from blueferry_calendar.caldav import CalDavClient
from blueferry_calendar.settings import SettingsStore
from blueferry_calendar.surfaces import load_manifest

HAS_V12 = "card" in api.KNOWN_CAPABILITIES


def _args(**overrides) -> argparse.Namespace:
    values = dict(url="caldav.icloud.com", user=USER, calendars="work", range="today_tomorrow",
                  reminder="10", key_file=False, password_stdin=True, no_verify=False)
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def icloud():
    server = FakeServer("icloud")
    return server, (lambda url, user, password: CalDavClient(url, user, password, send=server))


def test_setup_checks_and_stores(tmp_path, monkeypatch, capsys, icloud) -> None:
    _server, factory = icloud
    monkeypatch.setattr(cli, "install_activation", lambda: [])
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    store = SettingsStore(tmp_path, secret=FakeSecret())
    assert cli.setup(_args(), store, client_factory=factory) == 0
    out = capsys.readouterr().out
    assert "Calendars: Work" in out and PASSWORD not in out
    settings = store.load()
    assert settings.url == "https://caldav.icloud.com/" and settings.calendars == ("work",)
    assert settings.reminder == "10" and store.password(settings) == PASSWORD


def test_setup_reports_a_missing_calendar(tmp_path, monkeypatch, capsys, icloud) -> None:
    _server, factory = icloud
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    store = SettingsStore(tmp_path, secret=FakeSecret())
    assert cli.setup(_args(calendars="Family"), store, client_factory=factory) == 1
    assert "available: Home, Work" in capsys.readouterr().err
    assert store.load() is None


def test_setup_refuses_plain_http(tmp_path) -> None:
    assert cli.setup(_args(url="http://dav.example.org"), SettingsStore(tmp_path)) == 2


def test_calendars_lists_and_marks_the_selection(tmp_path, monkeypatch, capsys, icloud) -> None:
    _server, factory = icloud
    monkeypatch.setattr(cli, "install_activation", lambda: [])
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    store = SettingsStore(tmp_path, secret=FakeSecret())
    cli.setup(_args(), store, client_factory=factory)
    capsys.readouterr()
    assert cli.list_calendars(store, client_factory=factory) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["  Home", "* Work"]


def test_status_without_settings(capsys) -> None:
    assert cli.main(["status"]) == 0
    assert "Not configured" in capsys.readouterr().out


@pytest.mark.skipif(HAS_V12, reason="plugin API 1.2 is installed")
def test_old_plugin_api_gets_a_clear_message() -> None:
    with pytest.raises(ManifestError, match=r"plugin API 1\.2"):
        load_manifest(manifest_text())


def test_install_activation_writes_manifest_and_service(tmp_path, monkeypatch) -> None:
    import blueferry.plugin_api.manifest as manifest_module

    # Until blueferry-plugin-api 1.2 is released, teach the parser the two
    # capabilities the way 1.2 will.
    monkeypatch.setattr(manifest_module, "KNOWN_CAPABILITIES",
                        api.KNOWN_CAPABILITIES | {"card", "notify"})
    written = cli.install_activation(tmp_path)
    manifest = (tmp_path / "blueferry" / "plugins" / f"{PLUGIN_ID}.plugin").read_text()
    assert "serve" in manifest and len(written) == 2
    assert load_manifest(manifest).capabilities == ("card", "notify")
