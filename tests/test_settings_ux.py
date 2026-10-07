"""plugin-api 1.3: "Test connection", "Sign in with Nextcloud", versions, log names."""
from __future__ import annotations

import json
import logging

import pytest
from blueferry_plugin_kit.auth.nextcloud import NextcloudLogin
from blueferry_plugin_kit.testing import FakeNextcloudLogin, TestCA, WsgiServer
from fakes import PASSWORD, USER, FakeSecret
from test_service import Env

from blueferry_calendar.settings import Settings, SettingsError, SettingsStore

FORM = {"url": "https://cloud.example.org", "username": USER, "password": PASSWORD,
        "calendars": "", "range": "today", "reminder": "off"}
CALENDAR_NAMES = ("Personal", "Contact birthdays")


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def test_test_connection_lists_calendars_and_stores_nothing(env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _service, host = env.service()
    result = host.test_config(FORM)
    assert result == {"ok": True,
                      "message": "Connected as alice; 2 calendars: Personal, Contact birthdays."}
    assert env.store.load() is None and env.secret.items == {}
    host.assert_never_sent(PASSWORD)
    for text in (PASSWORD, *CALENDAR_NAMES):
        assert text not in caplog.text


def test_test_connection_marks_the_password(env) -> None:
    _service, host = env.service()
    result = host.test_config({**FORM, "password": "wrong-password"})
    assert result["ok"] is False
    assert result["errors"] == {"password": "the server refused the user name or password"}
    assert "wrong-password" not in json.dumps(result)


def test_test_connection_uses_the_stored_password_only_for_its_server(env) -> None:
    env.configure()
    _service, host = env.service()
    form = {key: value for key, value in FORM.items() if key != "password"}
    assert host.test_config(form)["ok"] is True
    other = host.test_config({**form, "url": "https://other.example.org"})
    assert other["ok"] is False and "password" in other["errors"]
    assert not any("other.example.org" in url for url in env.server.urls())
    host.assert_never_sent(PASSWORD)


def test_test_connection_names_a_missing_calendar(env) -> None:
    _service, host = env.service()
    result = host.test_config({**FORM, "calendars": "Holidays"})
    assert result["ok"] is False and "Holidays" in result["errors"]["calendars"]


def test_long_calendar_lists_are_cut_to_one_line() -> None:
    from blueferry_plugin_kit.dav.caldav import CalendarInfo

    from blueferry_calendar.service import calendars_message

    calendars = [CalendarInfo(url=f"https://x/{i}", name=f"Calendar number {i}")
                 for i in range(30)]
    text = calendars_message("anna", calendars)
    assert len(text) <= 200 and text.startswith("Connected as anna; 30 calendars: ")
    assert text.endswith("…")


def test_settings_error_is_logged_by_its_own_name(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    env = Env(tmp_path)
    env.store = SettingsStore(tmp_path / "config-file", secret=FakeSecret(fail=True))
    env.configure()
    env.store.key_path.unlink()          # the password file vanished
    with pytest.raises(SettingsError):
        env.store.password(env.store.load())
    service, host = env.service()
    host.card()
    assert "calendar refresh failed: SettingsError" in caplog.text
    assert "SecretsError" not in caplog.text
    assert "password file is missing" in service.status()["detail"]


# ---- Sign in with Nextcloud ---------------------------------------------------------


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> TestCA:
    return TestCA(tmp_path_factory.mktemp("ca"))


@pytest.fixture
def nextcloud(ca):
    # The fake names cloud.example.org as its server, where the CalDAV
    # fixtures live; the flow itself runs over https on 127.0.0.1.
    login = FakeNextcloudLogin(login_name=USER, app_password=PASSWORD)
    login.server = "https://cloud.example.org"
    with WsgiServer(login, ca=ca) as server:
        login.url = server.base
        yield login


def _signed_in_env(tmp_path, ca) -> tuple[Env, object, object]:
    env = Env(tmp_path)
    login = NextcloudLogin(user_agent="BlueFerry Calendar", context=ca.client_context(),
                           min_interval=0)
    from blueferry.plugin_api.testing import inline_service
    from fakes import FakeHost, plugin_manifest
    from test_service import ZURICH

    from blueferry_calendar.service import CalendarService

    service = inline_service(
        CalendarService, plugin_manifest(), settings=env.store, cache=env.cache,
        client_factory=env.factory, now=env.clock, zone=ZURICH, every=None, login=login,
    )
    return env, service, FakeHost(service, env.cache_dir)


def test_sign_in_with_nextcloud(tmp_path, ca, nextcloud, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    env, _service, host = _signed_in_env(tmp_path, ca)
    step = host.sign_in({"url": nextcloud.url, "calendars": "personal", "reminder": "10"},
                        before_poll=lambda n: n == 1 and nextcloud.grant())
    assert step == {"state": "done", 
                    "message": "Connected as alice; 2 calendars: Personal, Contact birthdays."}
    assert host.opened[0].startswith(nextcloud.url + "/login/v2/flow/")
    assert nextcloud.requests[0][2] == "BlueFerry Calendar"
    settings = env.store.load()
    assert settings == Settings(
        url="https://cloud.example.org/remote.php/dav/", username=USER, key_store="keyring",
        calendars=("personal",), range="today_tomorrow", reminder="10",
        hosts=("cloud.example.org",),
    )
    assert list(env.secret.items.values()) == [PASSWORD]
    assert host.card_changed >= 1
    assert host.card()[0]["title"] == "Team standup (moved)"
    host.assert_never_sent(PASSWORD)
    assert PASSWORD not in caplog.text
    assert not any(name in caplog.text for name in CALENDAR_NAMES)


def test_sign_in_cancel_and_errors(tmp_path, ca, nextcloud) -> None:
    env, _service, host = _signed_in_env(tmp_path, ca)
    step = host.config_login({"url": nextcloud.url})
    assert host.login_status(step["login_id"]) == {"state": "pending"}
    host.cancel_sign_in(step["login_id"])
    assert host.login_status(step["login_id"])["state"] == "cancelled"
    assert nextcloud.open_flows == 1          # Nextcloud forgets it after 20 minutes
    # http is refused before anything is sent.
    step = host.sign_in({"url": nextcloud.url.replace("https://", "http://")})
    assert step["state"] == "error"
    # A granted login whose calendars do not work is not stored.
    nextcloud.grant_after = 1
    nextcloud.app_password = "Other-App-Password-1"
    step = host.sign_in({"url": nextcloud.url})
    assert step == {"state": "error",
                    "message": "the server refused the user name or password"}
    assert env.store.load() is None
    host.assert_never_sent("Other-App-Password-1")
