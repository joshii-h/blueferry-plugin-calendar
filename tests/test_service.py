"""The plugin service against a fake v1.2 host and fake CalDAV servers."""
from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from blueferry.plugin_api.testing import inline_service
from blueferry_plugin_kit.dav.caldav import Response
from fakes import PASSWORD, USER, FakeHost, FakeSecret, FakeServer, plugin_manifest

from blueferry_calendar.cache import AgendaCache
from blueferry_calendar.service import CalendarService, new_client
from blueferry_calendar.settings import Settings, SettingsStore

ZURICH = ZoneInfo("Europe/Zurich")
UTC = timezone.utc
TITLES = ("Team standup", "Call with New York", "Anna's birthday", "Room 4", "meet.example.org")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Env:
    def __init__(self, tmp_path, server: str = "nextcloud", url: str = "https://cloud.example.org"):
        self.secret = FakeSecret()
        self.store = SettingsStore(tmp_path / "config", secret=self.secret)
        self.cache_dir = tmp_path / "cache" / "blueferry" / "calendar"
        self.cache = AgendaCache(self.cache_dir)
        self.clock = Clock(datetime(2026, 10, 6, 6, 0, tzinfo=UTC))   # 08:00 in Zurich
        self.server = FakeServer(server)
        self.url = url

    def factory(self, url, user, password, **options):
        return new_client(url, user, password, send=self.server, **options)

    def configure(self, **options) -> None:
        self.store.save(Settings(url=self.url, username=USER, **options), PASSWORD)

    def service(self) -> tuple[CalendarService, FakeHost]:
        service = inline_service(
            CalendarService, plugin_manifest(), settings=self.store, cache=self.cache,
            client_factory=self.factory, now=self.clock, zone=ZURICH, every=None,
        )
        return service, FakeHost(service, self.cache_dir)

    def reports(self) -> int:
        return len([m for m, *_ in self.server.requests if m == "REPORT"])


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def test_manifest_declares_card_notify_and_the_1_3_form() -> None:
    manifest = plugin_manifest()
    assert manifest.id == "io.weirdware.blueferry.calendar"
    assert set(manifest.capabilities) == {"card", "notify"}
    assert manifest.api_minor == 3 and manifest.config_test
    assert manifest.config_login == "nextcloud"
    fields = {f.key: f for f in manifest.config}
    assert list(fields) == ["url", "username", "password", "calendars", "range", "reminder",
                            "hosts", "use_system_proxy"]
    assert [g.name for g in manifest.config_groups] == ["account", "options", "advanced"]
    assert fields["use_system_proxy"].default is False
    assert fields["password"].secret and fields["password"].required
    assert fields["range"].choices == ("today", "today_tomorrow")
    assert fields["reminder"].choices == ("off", "10", "15")
    assert fields["reminder"].default == "off"


def test_unconfigured_card_points_to_the_settings(env) -> None:
    service, host = env.service()
    [item] = host.card()
    assert item["id"] == "setup" and "settings" in item["subtitle"]
    assert service.status()["state"] == "unconfigured"


def test_card_lists_the_next_events(env) -> None:
    env.configure()
    service, host = env.service()
    items = host.card()
    assert [(i["title"], i["subtitle"]) for i in items] == [
        ("Team standup (moved)", "Today 10:00–10:15 · Room 4"),
        ("Call with New York", "Today 17:00–18:00"),
        ("Anna's birthday", "Tomorrow, all day"),
    ]
    call = items[1]
    assert [(a["id"], a["kind"]) for a in call["actions"]] == [("open", "primary"),
                                                               ("refresh", "button")]
    assert [a["id"] for a in items[0]["actions"]] == ["refresh"]  # no link, no "open"
    assert service.status() == {"state": "ok", "server": "cloud.example.org"}
    assert host.card_changed == 1   # the first fetch announced itself


def test_range_today_and_past_events(env) -> None:
    env.configure(range="today")
    _service, host = env.service()
    env.clock.now = datetime(2026, 10, 6, 8, 5, tzinfo=UTC)   # 10:05, standup running
    titles = [(i["title"], i["subtitle"], i["icon"]) for i in host.card()]
    assert titles == [
        ("Team standup (moved)", "Now until 10:15 · Room 4", "appointment-soon"),
        ("Call with New York", "Today 17:00–18:00", "x-office-calendar"),
    ]
    env.clock.now = datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
    [empty] = host.card()
    assert empty["title"] == "No more events today"


def test_at_most_eight_items(env, tmp_path) -> None:
    events = "".join(
        f"BEGIN:VEVENT\r\nUID:e{n}\r\nDTSTAMP:20261001T000000Z\r\n"
        f"DTSTART:20261006T{10 + n:02d}0000Z\r\nDURATION:PT30M\r\nSUMMARY:Event {n}\r\n"
        "END:VEVENT\r\n" for n in range(12)
    )
    ics = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n{events}END:VCALENDAR\r\n"
    body = ('<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><response>'
            "<href>/a/b/x.ics</href><propstat><prop><C:calendar-data>"
            f"{ics}</C:calendar-data></prop><status>HTTP/1.1 200 OK</status></propstat>"
            "</response></multistatus>").encode()
    env.server = FakeServer("radicale")
    env.url = "http://localhost:5232"
    env.server.overrides[
        "REPORT http://localhost:5232/alice/0c6e0b1a-5f0e-4e4b-9b1c-2a7b5d1f9e10/"
    ] = Response(207, {}, body, "")
    env.configure()
    _service, host = env.service()
    items = host.card()
    assert len(items) == 8 and items[0]["title"] == "Event 0"


def test_open_and_refresh_actions(env) -> None:
    env.configure()
    _service, host = env.service()
    items = host.card()
    call = items[1]
    reply = host.invoke(call["id"], "open")
    assert reply == {"ok": True, "message": None, "open_uri": "https://meet.example.org/abc-def"}
    no_link = host.invoke(items[0]["id"], "open")
    assert no_link["ok"] is False and no_link["open_uri"] is None
    before, reports = host.card_changed, env.reports()
    reply = host.invoke(call["id"], "refresh", '{"ignored": true}')
    assert reply["ok"] and env.reports() > reports and host.card_changed == before + 1
    assert host.invoke(call["id"], "delete")["ok"] is False
    assert host.invoke("../x", "open")["ok"] is False
    assert host.invoke("ev-unknown", "open", "not json")["ok"] is False


def test_reminders_once_per_event_and_clickable(env) -> None:
    env.configure(reminder="10")
    service, host = env.service()
    service.tick()
    assert host.notifications == []                      # 08:00, standup at 10:00
    env.clock.now = datetime(2026, 10, 6, 7, 51, tzinfo=UTC)   # 09:51 local
    service.tick()
    [(title, body, icon, label, action_id)] = host.notifications
    assert title == "Team standup (moved)" and body == "In 9 min, 10:00–10:15 · Room 4"
    assert (icon, label, action_id) == ("appointment-soon", "", "")
    service.tick()
    assert len(host.notifications) == 1                  # not twice
    # A new process (bus activation) remembers what it already showed.
    again, again_host = env.service()
    again.tick()
    assert again_host.notifications == []
    env.clock.now = datetime(2026, 10, 6, 14, 52, tzinfo=UTC)  # 16:52, call at 17:00
    service.tick()
    title, body, _icon, label, action_id = host.notifications[-1]
    assert (title, label) == ("Call with New York", "Open") and body.startswith("In 8 min")
    assert host.click_notification()["open_uri"] == "https://meet.example.org/abc-def"


def test_reminders_off_and_all_day_events_never_notify(env) -> None:
    env.configure(reminder="off")
    service, host = env.service()
    env.clock.now = datetime(2026, 10, 6, 7, 55, tzinfo=UTC)
    service.tick()
    assert host.notifications == []
    env.configure(reminder="15")
    env.clock.now = datetime(2026, 10, 6, 21, 50, tzinfo=UTC)   # 23:50, birthday at 00:00
    service.tick()
    assert host.notifications == []


def test_polls_every_ten_minutes_and_backs_off_on_errors(env) -> None:
    env.configure()
    service, _host = env.service()
    service.tick()
    first = env.reports()
    assert first == 2
    env.clock.advance(minutes=5)
    service.tick()
    assert env.reports() == first
    env.clock.advance(minutes=5)
    service.tick()
    assert env.reports() == 2 * first
    # Server down: one try, then not again before two minutes have passed.
    env.server.password = "changed"
    env.clock.advance(minutes=10)
    service.tick()
    calls = len(env.server.requests)
    env.clock.advance(seconds=30)
    service.tick()
    assert len(env.server.requests) == calls
    assert service.status()["state"] == "error"
    env.clock.advance(minutes=2)
    service.tick()
    assert len(env.server.requests) > calls


def test_errors_show_on_the_card(env) -> None:
    env.server.password = "changed"
    env.configure()
    service, host = env.service()
    [item] = host.card()
    assert item["title"] == "Calendar unavailable"
    assert item["subtitle"] == "the server refused the user name or password"
    assert [a["id"] for a in item["actions"]] == ["refresh"]
    assert service.status()["detail"] == "the server refused the user name or password"
    reply = host.invoke(item["id"], "refresh")
    assert reply["ok"] is False and "refused" in reply["message"]


def test_cache_is_private_and_serves_a_new_process(env) -> None:
    env.configure()
    _service, host = env.service()
    first = host.card()
    assert stat.S_IMODE(os.stat(env.cache_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(env.cache_dir.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(env.cache.path).st_mode) == 0o600
    requests = len(env.server.requests)
    env.clock.advance(minutes=3)
    _again, again_host = env.service()
    assert again_host.card() == first
    assert len(env.server.requests) == requests          # answered from the cache
    env.clock.now = datetime(2026, 10, 7, 6, 0, tzinfo=UTC)   # next day: stale
    again_host.card()
    assert len(env.server.requests) > requests


def test_no_event_content_in_logs(env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    env.configure(reminder="15")
    service, host = env.service()
    host.card()
    env.clock.now = datetime(2026, 10, 6, 14, 50, tzinfo=UTC)
    service.tick()
    host.invoke(host.card()[0]["id"], "refresh")
    host.click_notification()
    assert host.notifications
    for text in TITLES + (PASSWORD,):
        assert text not in caplog.text


def test_settings_form_round_trip(env) -> None:
    _service, host = env.service()
    form = {"url": "https://cloud.example.org", "username": USER, "password": PASSWORD,
            "calendars": "personal, Holidays", "range": "today", "reminder": "15"}
    reply = json.loads(host._call("SetConfig", json.dumps(form)))
    assert reply["ok"] is False
    assert reply["errors"]["calendars"] == (
        "not found: Holidays; available: Personal, Contact birthdays"
    )
    reply = json.loads(host._call("SetConfig", json.dumps({**form, "password": "nope"})))
    assert reply["errors"] == {"password": "the server refused the user name or password"}
    form["calendars"] = "personal"
    assert json.loads(host._call("SetConfig", json.dumps(form))) == {"ok": True}
    values = json.loads(host._call("GetConfig"))["values"]
    assert values == {"url": "https://cloud.example.org", "username": USER,
                      "password": "********", "calendars": "personal", "range": "today",
                      "reminder": "15", "hosts": "", "use_system_proxy": False}
    # The allowlist holds the hosts discovery used: here only the server.
    assert env.store.load().hosts == ("cloud.example.org",)
    assert PASSWORD not in env.store.config_path.read_text()
    assert list(env.secret.items.values()) == [PASSWORD]
    # Only the selected calendar is fetched; the birthday calendar is not.
    assert [i["title"] for i in host.card()] == ["Team standup (moved)", "Call with New York"]
    # Changing only the reminder neither needs the password again nor the network.
    requests = len(env.server.requests)
    form.pop("password")
    form["reminder"] = "off"
    assert json.loads(host._call("SetConfig", json.dumps(form))) == {"ok": True}
    assert len(env.server.requests) == requests
    assert env.store.load().reminder == "off"


def test_keyring_fallback_file_is_private(tmp_path) -> None:
    store = SettingsStore(tmp_path / "config", secret=FakeSecret(fail=True))
    where = store.save(Settings(url="https://dav.example.org/", username=USER), PASSWORD)
    assert where == "file"
    assert stat.S_IMODE(os.stat(store.key_path).st_mode) == 0o600
    assert store.password(store.load()) == PASSWORD
    store.forget()
    assert store.load() is None and not store.key_path.exists()


def test_surfaces_live_on_plugin1() -> None:
    """Spec "D-Bus placement": no Card1/Notify1, everything on Plugin1."""
    for name in ("GetCardItems", "InvokeAction", "CardChanged", "Notify"):
        assert getattr(CalendarService, name)._dbus_interface == "io.weirdware.BlueFerry.Plugin1"


def test_the_form_needs_the_partition_host_confirmed(tmp_path) -> None:
    icloud = Env(tmp_path, "icloud", "https://caldav.icloud.com")
    _service, host = icloud.service()
    form = {"url": "https://caldav.icloud.com", "username": USER, "password": PASSWORD,
            "calendars": "", "range": "today", "reminder": "off"}
    reply = json.loads(host._call("SetConfig", json.dumps(form)))
    assert reply["ok"] is False and "p42-caldav.icloud.com" in reply["errors"]["hosts"]
    assert not any("p42" in url for url in icloud.server.urls())
    form["hosts"] = "p42-caldav.icloud.com"
    assert json.loads(host._call("SetConfig", json.dumps(form))) == {"ok": True}
    assert icloud.store.load().hosts == ("caldav.icloud.com", "p42-caldav.icloud.com")
    assert json.loads(host._call("GetConfig"))["values"]["hosts"] == "p42-caldav.icloud.com"
    form["hosts"] = "https://p42-caldav.icloud.com"
    reply = json.loads(host._call("SetConfig", json.dumps(form)))
    assert reply["errors"] == {"hosts": "must be host names separated by commas"}


def test_an_old_config_without_hosts_allows_only_its_own_host(env) -> None:
    env.configure()
    raw = json.loads(env.store.config_path.read_text())
    raw.pop("hosts")
    env.store.config_path.write_text(json.dumps(raw))
    settings = env.store.load()
    assert settings.hosts == ()
    client = env.factory(settings.url, USER, PASSWORD, hosts=settings.hosts)
    assert client.hosts == {"cloud.example.org"}
