"""iCal subscription links: the fetcher's rules, then the plugin against a
real https server (TestCA) serving a synthetic Google-like feed.

The feed (``fixtures/feeds/family.ics``) has a weekly RRULE with an EXDATE
and a moved instance (RECURRENCE-ID) in a Windows-named VTIMEZONE, a
multi-day all-day event, an UTC event, a monthly BYSETPOS rule in an Olson
zone without VTIMEZONE, a cancelled event, an old event and a VTODO.
"""
from __future__ import annotations

import gzip
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from blueferry.plugin_api.testing import inline_service
from blueferry_plugin_kit.dav.caldav import Response
from blueferry_plugin_kit.testing import TestCA, WsgiServer
from fakes import PASSWORD, USER, FakeHost, FakeSecret, plugin_manifest
from test_service import ZURICH, Env

from blueferry_calendar.feeds import (
    MAX_FEED_BYTES,
    FeedClient,
    FeedError,
    calendar_name,
    event_count,
    feed_id,
    looks_like_ical,
    normalize_link,
    parse_links,
)
from blueferry_calendar.service import CalendarService, new_client, new_feed_client
from blueferry_calendar.settings import FEEDS_SCHEMA, Settings

FEED = (Path(__file__).parent / "fixtures" / "feeds" / "family.ics").read_bytes()
SECRET = "private-7f3c9a1b2d4e6f8091a2b3c4d5e6f708"
PATH = f"/calendar/ical/anna%40example.org/{SECRET}/basic.ics"
UTC = timezone.utc

# ---- links -------------------------------------------------------------------------


def test_links_are_normalized() -> None:
    assert normalize_link("webcal://calendar.google.com/x/basic.ics") == (
        "https://calendar.google.com/x/basic.ics")
    assert normalize_link(" WEBCALS://p42-caldav.icloud.com/published/2/abc ") == (
        "https://p42-caldav.icloud.com/published/2/abc")
    assert normalize_link("outlook.office365.com/owa/calendar/a/b/calendar.ics") == (
        "https://outlook.office365.com/owa/calendar/a/b/calendar.ics")
    assert normalize_link("https://feeds.example.org/c.ics?token=1#top") == (
        "https://feeds.example.org/c.ics?token=1")
    assert normalize_link("http://localhost:8080/c.ics") == "http://localhost:8080/c.ics"
    assert normalize_link("http://school.example.org/c.ics", allow_http=True) == (
        "http://school.example.org/c.ics")


@pytest.mark.parametrize(("raw", "token"), [
    ("http://school.example.org/c.ics", "http-refused"),
    ("https://user:pw@example.org/c.ics", "invalid-url"),
    ("ftp://example.org/c.ics", "invalid-url"),
    ("https://", "invalid-url"),
    ("https://example.org:99999/c.ics", "invalid-url"),
    ("https://example.org/" + "a" * 2100, "invalid-url"),
])
def test_bad_links_are_refused(raw, token) -> None:
    with pytest.raises(FeedError) as caught:
        normalize_link(raw, index=2)
    assert caught.value.token == token and caught.value.index == 2
    assert raw not in caught.value.code and "c.ics" not in str(caught.value)


def test_several_links_by_position() -> None:
    links = parse_links("webcal://a.example.org/1.ics\n https://b.example.org/2.ics "
                        "https://a.example.org/1.ics")
    assert links == ("https://a.example.org/1.ics", "https://b.example.org/2.ics")
    with pytest.raises(FeedError) as caught:
        parse_links("https://a.example.org/1 http://b.example.org/2")
    assert (caught.value.index, caught.value.token) == (1, "http-refused")
    with pytest.raises(FeedError) as caught:
        parse_links(" ".join(f"https://x.example.org/{n}" for n in range(9)))
    assert caught.value.token == "too-many"
    assert feed_id(links[0]).startswith("ical-") and "a.example.org" not in feed_id(links[0])


def test_calendar_name_and_event_count() -> None:
    text = FEED.decode()
    assert calendar_name(text) == "Family, private"
    folded = "BEGIN:VCALENDAR\r\nX-WR-CALNAME;VALUE=TEXT:Ferien\r\n  Bern\r\nEND:VCALENDAR\r\n"
    assert calendar_name(folded) == "Ferien Bern"
    assert calendar_name("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n") == ""
    assert event_count(text) == 6        # the moved instance is no new event
    assert looks_like_ical("﻿\r\n" + text)
    assert not looks_like_ical("<!DOCTYPE html><html>sign in</html>")
    assert not looks_like_ical(text[:500])    # cut off


# ---- the fetcher, with a fake transport -----------------------------------------------


class FakeGet:
    def __init__(self, routes: dict[str, Response]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url, headers, timeout) -> Response:
        assert timeout <= 30
        self.requests.append((url, dict(headers)))
        reply = self.routes.get(url)
        if isinstance(reply, Exception):
            raise reply
        return reply or Response(404, {}, b"", url)


def _ok(body: bytes = FEED, **headers: str) -> Response:
    return Response(200, {"content-type": "text/calendar", **headers}, body, "")


def test_redirects_follow_the_allowlist() -> None:
    get = FakeGet({
        "https://a.example.org/1": Response(302, {"location": "/2"}, b"", ""),
        "https://a.example.org/2": Response(301, {"location": "https://b.example.org/3"}, b"",
                                            ""),
        "https://b.example.org/3": _ok(),
    })
    with pytest.raises(FeedError) as caught:
        FeedClient(get=get).fetch("https://a.example.org/1", 0)
    assert (caught.value.token, caught.value.host) == ("foreign-host", "b.example.org")
    assert [url for url, _ in get.requests] == ["https://a.example.org/1",
                                                "https://a.example.org/2"]
    feed = FeedClient(get=get, hosts=("b.example.org",)).fetch("https://a.example.org/1", 0)
    assert feed.count == 6 and feed.hosts == {"b.example.org"}
    # Down to http only with the opt-in.
    get.routes["https://a.example.org/1"] = Response(
        302, {"location": "http://a.example.org/plain"}, b"", "")
    get.routes["http://a.example.org/plain"] = _ok()
    with pytest.raises(FeedError) as caught:
        FeedClient(get=get).fetch("https://a.example.org/1", 0)
    assert caught.value.token == "http-refused"
    assert FeedClient(get=get, allow_http=True).fetch("https://a.example.org/1", 0).count == 6
    # A loop ends.
    get.routes["https://a.example.org/1"] = Response(302, {"location": "/1"}, b"", "")
    with pytest.raises(FeedError) as caught:
        FeedClient(get=get).fetch("https://a.example.org/1", 0)
    assert caught.value.token == "redirect"


def test_revalidation_with_etag_and_last_modified() -> None:
    url = "https://a.example.org/1"
    get = FakeGet({url: _ok(etag='"v1"', **{"last-modified": "Tue, 06 Oct 2026 05:00:00 GMT"})})
    client = FeedClient(get=get, user_agent="test/1")
    first = client.fetch(url, 0)
    assert (first.etag, first.modified) == ('"v1"', "Tue, 06 Oct 2026 05:00:00 GMT")
    assert get.requests[0][1]["User-Agent"] == "test/1"
    assert "If-None-Match" not in get.requests[0][1]
    get.routes[url] = Response(304, {}, b"", url)
    assert client.fetch(url, 0, first) is first
    assert get.requests[1][1]["If-None-Match"] == '"v1"'
    assert get.requests[1][1]["If-Modified-Since"] == "Tue, 06 Oct 2026 05:00:00 GMT"
    with pytest.raises(FeedError):
        client.fetch(url, 0)        # a 304 without a copy is no calendar


@pytest.mark.parametrize(("reply", "token"), [
    (Response(404, {}, b"", ""), "not-found"),
    (Response(410, {}, b"", ""), "not-found"),
    (Response(403, {}, b"", ""), "forbidden"),
    (Response(500, {}, b"", ""), "server-error"),
    (Response(200, {}, b"<html>Sign in</html>", ""), "not-calendar"),
    (Response(200, {}, b"x" * (MAX_FEED_BYTES + 1), ""), "too-large"),
    (Response(200, {"content-encoding": "br"}, FEED, ""), "not-calendar"),
    (Response(200, {"content-encoding": "gzip"}, b"not gzip", ""), "not-calendar"),
    (OSError("timed out"), "network"),
])
def test_errors_are_tokens(reply, token) -> None:
    get = FakeGet({"https://a.example.org/1": reply})
    with pytest.raises(FeedError) as caught:
        FeedClient(get=get).fetch("https://a.example.org/1", 3)
    assert (caught.value.token, caught.value.index, caught.value.host) == (
        token, 3, "a.example.org")
    assert caught.value.code == f"feed|4|a.example.org|{token}"


def test_gzip_is_unpacked_within_the_limit() -> None:
    get = FakeGet({"https://a.example.org/1": _ok(gzip.compress(FEED),
                                                  **{"content-encoding": "gzip"})})
    assert FeedClient(get=get).fetch("https://a.example.org/1", 0).count == 6
    assert get.requests[0][1]["Accept-Encoding"] == "gzip"
    bomb = gzip.compress(b"BEGIN:VCALENDAR\r\n" + b" " * (MAX_FEED_BYTES + 10))
    get.routes["https://a.example.org/1"] = _ok(bomb, **{"content-encoding": "gzip"})
    with pytest.raises(FeedError) as caught:
        FeedClient(get=get).fetch("https://a.example.org/1", 0)
    assert caught.value.token == "too-large"


# ---- the plugin against a real https server ------------------------------------------


class FeedApp:
    """Serves the feed at its secret path, with ETag and optional gzip."""

    def __init__(self) -> None:
        self.body = FEED
        self.etag = '"v1"'
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def count(self) -> int:
        with self.lock:
            return len(self.requests)

    def __call__(self, environ, start_response):
        path = environ.get("REQUEST_URI", environ["PATH_INFO"]).split("?")[0]
        with self.lock:
            self.requests.append((path, environ.get("HTTP_IF_NONE_MATCH", "")))
        if path == "/old-address":
            start_response("301 Moved", [("Location", PATH)])
            return [b""]
        if path == "/elsewhere":
            start_response("302 Found", [("Location", "https://elsewhere.example.net/c.ics")])
            return [b""]
        if path != PATH:
            start_response("404 Not Found", [("Content-Type", "text/html")])
            return [b"<html>Not found</html>"]
        if environ.get("HTTP_IF_NONE_MATCH") == self.etag:
            start_response("304 Not Modified", [("ETag", self.etag)])
            return [b""]
        body, headers = self.body, [("Content-Type", "text/calendar; charset=UTF-8"),
                                    ("ETag", self.etag)]
        if "gzip" in environ.get("HTTP_ACCEPT_ENCODING", ""):
            body = gzip.compress(body)
            headers.append(("Content-Encoding", "gzip"))
        start_response("200 OK", [*headers, ("Content-Length", str(len(body)))])
        return [body]


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> TestCA:
    return TestCA(tmp_path_factory.mktemp("ca"))


@pytest.fixture
def server(ca):
    app = FeedApp()
    with WsgiServer(app, ca=ca) as running:
        running.app = app
        yield running


class FeedEnv(Env):
    def __init__(self, tmp_path, ca, server) -> None:
        super().__init__(tmp_path)
        self.ca = ca
        self.feed_server = server
        self.link = server.base + PATH
        self.webcal = self.link.replace("https://", "webcal://")

    def feed_factory(self, **options):
        return new_feed_client(context=self.ca.client_context(), **options)

    def service(self) -> tuple[CalendarService, FakeHost]:
        service = inline_service(
            CalendarService, plugin_manifest(), settings=self.store, cache=self.cache,
            client_factory=self.factory, feed_client_factory=self.feed_factory,
            now=self.clock, zone=ZURICH, every=None,
        )
        return service, FakeHost(service, self.cache_dir)

    def form(self, **values) -> dict:
        return {"use_caldav": False, "use_ical": True, "ical_urls": self.webcal,
                "range": "today_tomorrow", "reminder": "off", **values}


@pytest.fixture
def feed_env(tmp_path, ca, server) -> FeedEnv:
    return FeedEnv(tmp_path, ca, server)


CARD = [
    ("School holidays", "Today, all day"),
    ("Weekly sync (moved)", "Today 11:00–11:30 · Room 7"),
    ("Club call", "Tomorrow 00:00–01:00"),
    ("Flight to New York", "Tomorrow 08:00–09:00"),
]


def test_google_style_link_end_to_end(feed_env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    service, host = feed_env.service()
    result = host.test_config(feed_env.form())
    assert result == {"ok": True,
                      "message": "iCal: 6 events found, calendar “Family, private”."}
    assert feed_env.store.load() is None              # the test stores nothing
    assert feed_env.feed_server.app.count() == 1
    assert host.set_config(feed_env.form(reminder="15")) == {"ok": True}
    settings = feed_env.store.load()
    assert (settings.use_caldav, settings.use_ical) == (False, True)
    assert settings.feed_hosts == ("127.0.0.1",) and settings.hosts == ()
    # The link lives in the keyring, as the https form of the webcal link.
    [(schema, _attrs)] = [key for key in feed_env.secret.items if key[0] == FEEDS_SCHEMA]
    assert feed_env.secret.items[(schema, _attrs)] == feed_env.link
    assert "BlueFerry calendar iCal links" in feed_env.secret.labels
    assert host.get_config()["values"]["ical_urls"] == "********"
    assert [(i["title"], i["subtitle"]) for i in host.card()] == CARD
    assert feed_env.feed_server.app.count() == 2     # the check's fetch was reused
    flight = host.card()[3]
    assert host.invoke(flight["id"], "open")["open_uri"] == "https://airline.example.org/booking"
    assert service.status() == {"state": "ok", "server": "127.0.0.1"}
    # Reminders work like for CalDAV: 10:50, the moved sync starts at 11:00.
    feed_env.clock.now = datetime(2026, 10, 6, 8, 50, tzinfo=UTC)
    service.tick()
    assert host.notifications[-1][:2] == ("Weekly sync (moved)", "In 10 min, 11:00–11:30 · Room 7")
    # Nothing secret anywhere: logs, replies, notifications, config, cache.
    host.assert_never_sent(SECRET, feed_env.link)
    for where in (caplog.text, feed_env.store.config_path.read_text(),
                  feed_env.cache.path.read_text(), json.dumps(service.status())):
        assert SECRET not in where and "anna%40" not in where
    for title, _subtitle in CARD:
        assert title not in caplog.text


def test_fetches_every_15_minutes_with_revalidation(feed_env) -> None:
    service, host = feed_env.service()
    assert host.set_config(feed_env.form()) == {"ok": True}
    app = feed_env.feed_server.app
    assert app.count() == 1
    feed_env.clock.advance(minutes=11)        # the agenda is stale, the link not yet
    service.tick()
    assert app.count() == 1
    feed_env.clock.advance(minutes=11)
    service.tick()
    assert app.count() == 2 and app.requests[-1][1] == '"v1"'     # answered with 304
    assert [i["title"] for i in host.card()] == [title for title, _ in CARD]
    # "Refresh" asks at once; a changed feed replaces the agenda.
    app.body = FEED.replace(b"SUMMARY:Flight to New York", b"SUMMARY:Flight to Boston")
    app.etag = '"v2"'
    reply = host.invoke(host.card()[0]["id"], "refresh")
    assert reply["ok"] is True and app.count() == 3
    assert "Flight to Boston" in [i["title"] for i in host.card()]


def test_link_errors_name_the_link_but_never_show_it(feed_env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _service, host = feed_env.service()
    wrong = feed_env.feed_server.base + PATH.replace(SECRET, "private-0000")
    result = host.test_config(feed_env.form(ical_urls=f"{feed_env.link} {wrong}"))
    assert result["ok"] is False
    assert result["errors"] == {"ical_urls": (
        "iCal link 2 (127.0.0.1): the address is not (or no longer) valid; copy it again "
        "(Google changes it when the secret address is reset)")}
    result = host.test_config(feed_env.form(ical_urls=feed_env.feed_server.base + "/elsewhere"))
    assert result["errors"] == {"hosts": (
        "iCal link 1 redirects to elsewhere.example.net; add it under 'More allowed hosts' "
        "if it belongs to the calendar provider")}
    result = host.test_config(feed_env.form(ical_urls="http://calendar.example.org/x.ics"))
    assert "plain http is off" in result["errors"]["ical_urls"]
    result = host.test_config(feed_env.form(ical_urls=""))
    assert result["ok"] is False and "ical_urls" in result["errors"]
    result = host.test_config(feed_env.form(use_ical=False))
    assert result["errors"] == {"use_caldav": "switch on the CalDAV account, iCal links or both"}
    # A moved address on the same host is followed.
    moved = host.test_config(feed_env.form(ical_urls=feed_env.feed_server.base + "/old-address"))
    assert moved["ok"] is True
    host.assert_never_sent(SECRET, "private-0000")
    assert SECRET not in caplog.text and "private-0000" not in caplog.text


def test_stored_link_is_kept_until_replaced(feed_env) -> None:
    _service, host = feed_env.service()
    assert host.set_config(feed_env.form()) == {"ok": True}
    form = feed_env.form(ical_names="Family")
    form.pop("ical_urls")                   # the form never shows the link again
    assert host.test_config(form)["message"] == "iCal: 6 events found, calendar “Family”."
    assert host.set_config(form) == {"ok": True}
    assert feed_env.store.feeds(feed_env.store.load()) == (feed_env.link,)
    assert {i["title"] for i in host.card()} >= {"Flight to New York"}
    feed_env.store.forget()
    assert feed_env.secret.items == {} and not feed_env.store.feed_secrets.key_path.exists()


def test_feed_fallback_file_is_private(tmp_path) -> None:
    from blueferry_calendar.settings import SettingsStore

    store = SettingsStore(tmp_path / "config", secret=FakeSecret(fail=True))
    assert store.save_feeds(("https://a.example.org/1",)) == "file"
    assert oct(store.feed_secrets.key_path.stat().st_mode & 0o777) == "0o600"
    settings = Settings(url="", username="", use_caldav=False, use_ical=True,
                        feeds_id="x", feeds_store="file")
    assert store.feeds(settings) == ("https://a.example.org/1",)


def test_caldav_and_ical_together(feed_env) -> None:
    service, host = feed_env.service()
    form = feed_env.form(use_caldav=True, url="https://cloud.example.org", username=USER,
                         password=PASSWORD, calendars="personal", ical_names="Family")
    result = host.test_config(form)
    assert result["ok"] is True
    assert result["message"].startswith(
        "Connected as alice; 2 calendars: Personal, Contact birthdays. iCal: 6 events")
    assert host.set_config(form) == {"ok": True}
    titles = [i["title"] for i in host.card()]
    assert "Team standup (moved)" in titles and "Weekly sync (moved)" in titles
    assert service.status()["server"] == "cloud.example.org, 127.0.0.1"
    # The CalDAV account fails: the iCal events stay, Status names the problem.
    feed_env.server.password = "changed"
    feed_env.clock.advance(minutes=11)
    reply = host.invoke(host.card()[0]["id"], "refresh")
    assert reply == {"ok": False, "message": "the server refused the user name or password",
                     "open_uri": None}
    titles = [i["title"] for i in host.card()]
    assert "Weekly sync (moved)" in titles and "Team standup (moved)" not in titles
    assert service.status()["state"] == "error"
    # The link fails (gone) while CalDAV works: the last copy stays.
    feed_env.server.password = PASSWORD
    feed_env.feed_server.app.etag = '"v3"'
    feed_env.feed_server.app.body = b"<html>gone</html>"
    reply = host.invoke(host.card()[0]["id"], "refresh")
    assert reply["ok"] is False
    assert reply["message"] == (
        "iCal link 1 (127.0.0.1): this address does not deliver an iCal calendar (.ics)")
    titles = [i["title"] for i in host.card()]
    assert "Team standup (moved)" in titles and "Weekly sync (moved)" in titles


def test_german_messages(feed_env, monkeypatch) -> None:
    monkeypatch.setenv("LANG", "de_CH.UTF-8")
    _service, host = feed_env.service()
    result = host.test_config(feed_env.form())
    assert result["message"] == "iCal: 6 Termine gefunden, Kalender „Family, private“."
    result = host.test_config(feed_env.form(ical_urls=feed_env.feed_server.base + "/nope"))
    assert result["errors"]["ical_urls"].startswith("iCal-Link 1 (127.0.0.1): die Adresse")


def test_ical_only_without_caldav_settings_loads(feed_env) -> None:
    feed_env.store.save_options(Settings(url="", username="", use_caldav=False, use_ical=True))
    assert feed_env.store.load().use_caldav is False
    service, host = feed_env.service()
    # No links stored: the card says so instead of crashing.
    [item] = host.card()
    assert item["title"] == "Calendar unavailable"
    assert service.status()["state"] == "error"


def test_the_feed_client_factory_gets_the_network_options(feed_env) -> None:
    seen = []

    def factory(**options):
        seen.append(options)
        return feed_env.feed_factory(**options)

    service = inline_service(
        CalendarService, plugin_manifest(), settings=feed_env.store, cache=feed_env.cache,
        client_factory=lambda *a, **k: new_client(*a, send=feed_env.server, **k),
        feed_client_factory=factory, now=feed_env.clock, zone=ZURICH, every=None,
    )
    host = FakeHost(service, feed_env.cache_dir)
    assert host.set_config(feed_env.form(use_system_proxy=False, hosts="cdn.example.org",
                                         allow_http_feeds=True)) == {"ok": True}
    assert seen[0] == {"hosts": ("cdn.example.org",), "allow_http": True, "use_proxy": False}
    # Hosts that the check did not need are not kept.
    assert feed_env.store.load().hosts == ()


def test_window_cache_is_per_window(feed_env) -> None:
    service, host = feed_env.service()
    assert host.set_config(feed_env.form()) == {"ok": True}
    feed_env.clock.now = datetime(2026, 10, 7, 6, 0, tzinfo=UTC)   # next day
    service.refresh()
    titles = [i["title"] for i in host.card()]
    # Wednesday's weekly sync is excluded (EXDATE); Thursday has none.
    assert "Weekly sync" not in titles and "Flight to New York" in titles
    feed_env.clock.now += timedelta(days=6)                         # Tue 13 Oct
    service.refresh()
    assert "Weekly sync" in [i["title"] for i in host.card()]

