"""The plugin process: a CalDAV and/or iCal agenda as a card, reminders as
notifications.

Threads: D-Bus calls and the timer run on the GLib main loop; network and
keyring work runs on worker threads (``run_async`` or ``start_worker``),
and signals are emitted back on the main loop. The agenda is fetched every
ten minutes and on request ("Refresh"); iCal subscription links at most every
15 minutes (revalidated with ETag/Last-Modified, kept in memory only). The
card answers from memory or the cache at once and refreshes in the
background when the data is stale.

Nothing about an event (title, place, time, link) is ever logged, and no
iCal link either (each one is a secret).
"""
from __future__ import annotations

import functools
import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any

import dbus
import dbus.service
from blueferry.plugin_api.config import ConfigError
from blueferry.plugin_api.config_flow import MAX_MESSAGE, ConfigTestResult, LoginStep
from blueferry.plugin_api.manifest import PluginManifest
from blueferry.plugin_api.service import PluginCallError, PluginService
from blueferry_plugin_kit.auth.nextcloud import MESSAGES as LOGIN_MESSAGES
from blueferry_plugin_kit.auth.nextcloud import Credentials, NextcloudLogin
from blueferry_plugin_kit.configtest import passed
from blueferry_plugin_kit.dav.caldav import (
    CalDavClient,
    CalDavError,
    CalendarInfo,
    host_of,
    normalize_url,
    split_hosts,
    valid_host,
)
from blueferry_plugin_kit.dav.ical import Occurrence, local_zone, occurrences

from blueferry_calendar import __version__
from blueferry_calendar.cache import AgendaCache, Snapshot
from blueferry_calendar.feeds import (
    FETCH_EVERY,
    Feed,
    FeedClient,
    FeedError,
    feed_id,
    fingerprint,
    normalize_link,
    parse_links,
)
from blueferry_calendar.i18n import t
from blueferry_calendar.settings import (
    Settings,
    SettingsError,
    SettingsStore,
    split_feed_names,
    split_names,
)
from blueferry_calendar.surfaces import (
    ID,
    MAX_ITEMS,
    NOTIFY_ITEM,
    SURFACE_INTERFACE,
    action,
    action_reply,
    card_item,
    card_reply,
    parse_args,
)

log = logging.getLogger(__name__)

# A CalDAV client that names this plugin in its User-Agent.
new_client: Callable[..., CalDavClient] = functools.partial(
    CalDavClient, user_agent=f"blueferry-calendar/{__version__}",
)

# An iCal link fetcher with the same User-Agent.
new_feed_client: Callable[..., FeedClient] = functools.partial(
    FeedClient, user_agent=f"blueferry-calendar/{__version__}",
)

REFRESH_EVERY = timedelta(minutes=10)
RETRY_AFTER = timedelta(minutes=2)
TICK_SECONDS = 30
NOTIFIED_KEEP = timedelta(days=2)


def error_text(token: str) -> str:
    """The user's-language text for a CalDAV error token or an iCal link's
    error code (``feed|n|host|token``); else the token."""
    if token.startswith("feed|"):
        _feed, number, host, kind = (token.split("|", 3) + ["", "", ""])[:4]
        key = "feed_err_" + kind
        text = t(key)
        return f"{feed_label(number, host)}: {kind if text == key else text}"
    key = "err_" + token
    text = t(key)
    return token if text == key else text


def feed_label(number: object, host: str = "") -> str:
    """``iCal link 2 (calendar.google.com)``; never the link itself."""
    return t("feed_label", number=number) + (f" ({host})" if host else "")


def problem_code(error: CalDavError) -> str:
    return error.code if isinstance(error, FeedError) else error.token


def feed_config_error(error: FeedError) -> ConfigError:
    """The settings field an iCal link's error is about, with a reason."""
    if error.token == "foreign-host" and error.host:
        return ConfigError("hosts", t(
            "feed_foreign_host", label=feed_label(error.index + 1), host=error.host,
        ))
    return ConfigError("ical_urls", error_text(error.code))


def login_messages() -> dict[str, str]:
    """The Nextcloud sign-in's messages in the user's language."""
    return {key: t(f"login_{key}") for key in LOGIN_MESSAGES}


Every = Callable[[int, Callable[[], bool]], object]


def _every(seconds: int, callback: Callable[[], bool]) -> object:
    from gi.repository import GLib

    return GLib.timeout_add_seconds(seconds, callback)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def select(calendars: list[CalendarInfo], names: tuple[str, ...]) -> tuple[
    list[CalendarInfo], list[str],
]:
    """The calendars named (by display name, case-insensitively, or by URL)."""
    if not names:
        return list(calendars), []
    chosen: dict[str, CalendarInfo] = {}
    missing = []
    for name in names:
        wanted = name.casefold()
        hits = [c for c in calendars if c.name.casefold() == wanted or c.url == name]
        if not hits:
            missing.append(name)
        for hit in hits:
            chosen.setdefault(hit.url, hit)
    return list(chosen.values()), missing


def window(now: datetime, zone: tzinfo) -> tuple[datetime, datetime]:
    """Start of today to the end of tomorrow, in the local zone."""
    today = now.astimezone(zone).date()
    start = datetime.combine(today, time(), tzinfo=zone)
    end = datetime.combine(today + timedelta(days=2), time(), tzinfo=zone)
    return start, end


def extra_hosts(settings: Settings) -> tuple[str, ...]:
    """The allowed hosts besides the server's own (what the form shows)."""
    own = host_of(settings.url)
    return tuple(host for host in settings.hosts if host != own)


def connect(
    client_factory: Callable[..., CalDavClient], settings: Settings, password: str,
) -> CalDavClient:
    return client_factory(settings.url, settings.username, password, hosts=settings.hosts,
                          use_proxy=settings.use_system_proxy)


def discover(
    client_factory: Callable[..., CalDavClient], settings: Settings, password: str,
) -> tuple[list[CalendarInfo], tuple[str, ...]]:
    """The calendars and every host discovery used (principal, home,
    calendars). Raises CalDavError, ``foreign-host`` naming the host."""
    client = connect(client_factory, settings, password)
    calendars = client.calendars()
    used = client.seen_hosts | {host_of(c.url) for c in calendars} | {host_of(settings.url)}
    return calendars, tuple(sorted(used))


def config_error(error: CalDavError) -> ConfigError:
    """The settings field a discovery error is about, with a reason."""
    if error.token == "foreign-host" and error.host:
        return ConfigError("hosts", t("foreign_host", host=error.host))
    field = "password" if error.token in ("unauthorized", "forbidden") else "url"
    return ConfigError(field, error_text(error.token))


def check_selection(calendars: list[CalendarInfo], settings: Settings) -> None:
    if not calendars:
        raise ConfigError("url", error_text("no-calendars"))
    _chosen, missing = select(calendars, settings.calendars)
    if missing:
        available = ", ".join(c.name for c in calendars)
        raise ConfigError(
            "calendars", t("not_found", missing=", ".join(missing), available=available)[:300],
        )


def verify(
    client_factory: Callable[..., CalDavClient], settings: Settings, password: str,
) -> tuple[list[CalendarInfo], tuple[str, ...]]:
    """Discovery with these settings; ConfigError names the bad field."""
    try:
        calendars, hosts = discover(client_factory, settings, password)
    except CalDavError as error:
        raise config_error(error) from None
    check_selection(calendars, settings)
    return calendars, hosts


def calendars_message(user: str, calendars: list[CalendarInfo]) -> str:
    """``Connected as anna; 3 calendars: Private, Work, Family.`` (≤ 200)."""
    names = [c.name for c in calendars]
    count = t("calendars_one" if len(names) == 1 else "calendars_many", count=len(names))
    head = f"{t('connected', user=user)}; {count}: "
    text = head + ", ".join(names) + "."
    if len(text) > MAX_MESSAGE:
        text = text[:MAX_MESSAGE - 1].rstrip(", ") + "…"
    return text


def feed_name(settings: Settings, index: int, feed: Feed | None, link: str) -> str:
    """The user's name for a link, else the feed's X-WR-CALNAME, else its host."""
    if index < len(settings.feed_names) and settings.feed_names[index]:
        return settings.feed_names[index]
    if feed is not None and feed.name:
        return feed.name
    return host_of(link)


def verify_feeds(
    client_factory: Callable[..., FeedClient], settings: Settings, links: tuple[str, ...],
) -> tuple[list[Feed], set[str]]:
    """Fetch every link once; the feeds and the other hosts redirects used.
    Raises ConfigError naming the field (the link never appears)."""
    client = client_factory(hosts=settings.hosts, allow_http=settings.allow_http_feeds,
                            use_proxy=settings.use_system_proxy)
    feeds: list[Feed] = []
    used: set[str] = set()
    for index, link in enumerate(links):
        try:
            feed = client.fetch(link, index)
        except FeedError as error:
            raise feed_config_error(error) from None
        feeds.append(feed)
        used |= feed.hosts
    return feeds, used


def feeds_message(settings: Settings, links: tuple[str, ...], feeds: list[Feed]) -> str:
    """``iCal: 743 events found, calendar “Private”; …``."""
    parts = [
        t("feed_found_one" if feed.count == 1 else "feed_found_many", count=feed.count,
          name=feed_name(settings, index, feed, link))
        for index, (link, feed) in enumerate(zip(links, feeds, strict=True))
    ]
    return "iCal: " + "; ".join(parts) + "."


def one_line(parts: list[str]) -> str:
    text = " ".join(parts)
    if len(text) > MAX_MESSAGE:
        text = text[:MAX_MESSAGE - 1].rstrip(", ;") + "…"
    return text


def _source(settings: Settings) -> str:
    """Which settings an agenda belongs to (a hash, nothing readable)."""
    parts = [settings.url, settings.username, *settings.calendars] if settings.use_caldav else []
    if settings.use_ical:   # CalDAV-only configs keep the hash they had
        parts += ["\x00ical", settings.feeds_id, *settings.feed_names]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


class CalendarService(PluginService):
    def __init__(
        self,
        manifest: PluginManifest,
        bus: Any = None,
        *,
        settings: SettingsStore | None = None,
        cache: AgendaCache | None = None,
        client_factory: Callable[..., CalDavClient] = new_client,
        feed_client_factory: Callable[..., FeedClient] = new_feed_client,
        now: Callable[[], datetime] = _utc_now,
        zone: tzinfo | None = None,
        every: Every | None = _every,
        login: NextcloudLogin | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(manifest, bus, **kwargs)
        self._login = login or NextcloudLogin(
            user_agent="BlueFerry Calendar", messages=login_messages(),
        )
        self._login_values: dict[str, dict[str, object]] = {}
        self._settings = settings or SettingsStore()
        self._cache = cache or AgendaCache()
        self._client_factory = client_factory
        self._feed_client_factory = feed_client_factory
        # iCal links (the keys are secrets: memory only, never logged).
        # Only touched while holding _refresh_lock.
        self._feeds: dict[str, Feed] = {}
        self._feed_times: dict[str, datetime] = {}
        self._now = now
        self._zone = zone or local_zone()
        self._every = every
        self._lock = threading.Lock()            # guards the fields below
        self._refresh_lock = threading.Lock()    # one fetch at a time
        self._snapshot: Snapshot | None = None
        self._refreshing = False
        self._last_error = ""
        self._attempt: datetime | None = None

    # ---- lifecycle --------------------------------------------------------------

    def start(self) -> CalendarService:
        """Start the timer and the first refresh (main loop)."""
        if self._every is not None:
            self._every(TICK_SECONDS, self.tick)
        self.tick()
        return self

    def tick(self) -> bool:
        """Main loop, every 30 s: refresh when stale, show due reminders."""
        try:
            settings = self._settings.load()
        except SettingsError:
            settings = None
        if settings is None:
            return True
        if settings.reminder_minutes:
            # Reminders need a running process: do not idle out.
            self.last_activity = self._clock()
        if self._due(settings):
            self._refresh_in_background()
        self.check_reminders(settings)
        return True

    def _stale(self, settings: Settings) -> bool:
        snapshot = self._current()
        if snapshot.fetched_at is None or snapshot.source != _source(settings):
            return True
        age = self._now() - snapshot.fetched_at
        return age >= REFRESH_EVERY or age < timedelta(0) or (
            self._window(self._now())[0] > snapshot.fetched_at
        )

    def _due(self, settings: Settings) -> bool:
        """Stale, and not tried within the last two minutes (backoff on errors)."""
        if not self._stale(settings):
            return False
        with self._lock:
            attempt = self._attempt
        return attempt is None or not timedelta(0) <= self._now() - attempt < RETRY_AFTER

    def _current(self) -> Snapshot:
        with self._lock:
            if self._snapshot is None:
                self._snapshot = self._cache.load()
            return self._snapshot

    def _refresh_in_background(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
            self._attempt = self._now()

        def work() -> None:
            try:
                changed = self.refresh()
            except Exception as error:  # reported in Status() and on the card
                log.info("calendar refresh failed: %s", getattr(error, "token", "") or
                         type(error).__name__)
                changed = True
            finally:
                with self._lock:
                    self._refreshing = False
            if changed:
                self._to_main(self._card_changed)

        self._start_worker(work)

    # ---- fetching (worker thread) ----------------------------------------------------

    def _window(self, now: datetime) -> tuple[datetime, datetime]:
        return window(now, self._zone)

    def refresh(self, *, force_feeds: bool = False) -> bool:
        """Fetch the window's events; True when the agenda changed. Blocking.

        With both sources, one that fails does not hide the other: its error
        shows in Status() and the agenda keeps what worked (an iCal link
        that fails keeps its last copy from this process)."""
        with self._refresh_lock:
            try:
                settings = self._settings.load()
                if settings is None:
                    raise CalDavError("unconfigured")
                now = self._now()
                start, end = self._window(now)
                events: list[Occurrence] = []
                problems: list[CalDavError] = []
                sources = 0
                if settings.use_caldav:
                    try:
                        sources += self._caldav_events(settings, start, end, events)
                    except CalDavError as error:
                        if not settings.use_ical:
                            raise
                        problems.append(error)
                if settings.use_ical:
                    sources += self._feed_events(
                        settings, now, (start, end), events, problems, force_feeds,
                    )
                if not sources:
                    raise problems[0] if problems else CalDavError("no-calendars")
            except SettingsError as error:
                with self._lock:
                    self._last_error = str(error)
                raise
            except CalDavError as error:
                with self._lock:
                    self._last_error = problem_code(error)
                raise
            events.sort(key=lambda item: (item.start_at, item.title))
            problem = problem_code(problems[0]) if problems else ""
            with self._lock:
                previous = self._snapshot or self._cache.load()
                keep_after = now - NOTIFIED_KEEP
                notified = {
                    key: when for key, when in previous.notified.items()
                    if _parse(when) is not None and _parse(when) >= keep_after
                }
                snapshot = Snapshot(
                    fetched_at=now, source=_source(settings), events=events, notified=notified,
                )
                changed = (
                    [e.to_json() for e in previous.events] != [e.to_json() for e in events]
                    or self._last_error != problem
                )
                self._snapshot = snapshot
                self._last_error = problem
            self._save(snapshot)
            log.info("calendar refreshed: %d calendars, %d events", sources, len(events))
            return changed

    def _caldav_events(
        self, settings: Settings, start: datetime, end: datetime, events: list[Occurrence],
    ) -> int:
        """Add the CalDAV events; the number of calendars read."""
        password = self._settings.password(settings)
        client = connect(self._client_factory, settings, password)
        found, missing = select(client.calendars(), settings.calendars)
        if not found:
            raise CalDavError("missing-calendars" if missing else "no-calendars")
        failures = 0
        last: CalDavError | None = None
        for calendar in found:
            try:
                texts = client.events(calendar.url, start, end)
            except CalDavError as error:
                if error.token in ("unauthorized", "network"):
                    raise
                failures += 1   # one shared calendar may refuse; keep the rest
                last = error
                continue
            events += occurrences(
                texts, start, end, self._zone, calendar=calendar.name,
                calendar_id=calendar.url,
            )
        if failures == len(found) and last is not None:
            raise last
        return len(found) - failures

    def _feed_events(
        self, settings: Settings, now: datetime, span: tuple[datetime, datetime],
        events: list[Occurrence], problems: list[CalDavError], force: bool,
    ) -> int:
        """Add the iCal links' events; the number of links with data.

        A link is fetched when its copy is 15 minutes old (or on "Refresh");
        otherwise the copy in memory is expanded again (cached per window)."""
        start, end = span
        links = self._settings.feeds(settings)
        client = self._feed_client_factory(
            hosts=settings.hosts, allow_http=settings.allow_http_feeds,
            use_proxy=settings.use_system_proxy,
        )
        kept: dict[str, Feed] = {}
        with_data = 0
        for index, link in enumerate(links):
            feed = self._feeds.get(link)
            fetched = self._feed_times.get(link)
            if force or feed is None or fetched is None or not (
                timedelta(0) <= now - fetched < FETCH_EVERY
            ):
                try:
                    feed = client.fetch(link, index, feed)
                    self._feed_times[link] = now
                except FeedError as error:
                    log.info("iCal link %d failed: %s", index + 1, error.token)
                    problems.append(error)
            if feed is None:
                continue
            kept[link] = feed
            name = feed_name(settings, index, feed, link)
            key = (start.isoformat(), end.isoformat(), name)
            expanded = feed.expanded.get(key)
            if expanded is None:
                expanded = occurrences([feed.text], start, end, self._zone, calendar=name,
                                       calendar_id=feed_id(link))
                feed.expanded = {key: expanded}
            events += expanded
            with_data += 1
        self._feeds = kept
        self._feed_times = {link: when for link, when in self._feed_times.items()
                            if link in kept}
        return with_data

    def _save(self, snapshot: Snapshot) -> None:
        try:
            self._cache.save(snapshot)
        except (OSError, SettingsError) as error:
            log.info("could not write the agenda cache (%s)", type(error).__name__)

    # ---- reminders (main loop) ----------------------------------------------------

    def check_reminders(self, settings: Settings) -> None:
        minutes = settings.reminder_minutes
        if not minutes:
            return
        now = self._now()
        due: list[Occurrence] = []
        with self._lock:
            snapshot = self._snapshot
            if snapshot is None or snapshot.source != _source(settings):
                return
            for event in snapshot.events:
                if event.all_day or event.key in snapshot.notified:
                    continue
                lead = event.start_at - now
                if timedelta(0) < lead <= timedelta(minutes=minutes):
                    snapshot.notified[event.key] = event.start
                    due.append(event)
        if not due:
            return
        self._save(snapshot)
        for event in due:
            self._notify(event, now)

    def _notify(self, event: Occurrence, now: datetime) -> None:
        minutes = max(1, round((event.start_at - now).total_seconds() / 60))
        local = event.start_at.astimezone(self._zone)
        end = event.end_at.astimezone(self._zone)
        body = t("reminder", minutes=minutes, start=f"{local:%H:%M}", end=f"{end:%H:%M}")
        if event.location:
            body += f" · {event.location}"
        label, action_id = (t("open"), f"open-{event.key}") if event.url else ("", "")
        self.Notify(event.title, body, "appointment-soon", label, action_id)
        log.info("reminder shown")

    # ---- card ---------------------------------------------------------------------

    def card_items(self) -> list[dict[str, object]]:
        """The card: the next events of the configured range. Worker thread."""
        try:
            settings = self._settings.load()
        except SettingsError as error:
            return [self._message_item("error", t("settings_unreadable"), str(error))]
        if settings is None:
            return [card_item(
                "setup", t("not_set_up"), icon="x-office-calendar",
                subtitle=t("not_set_up_hint"),
            )]
        if self._due(settings):
            self._refresh_in_background()
        snapshot = self._current()
        with self._lock:
            error = self._last_error
            refreshing = self._refreshing
        if snapshot.source != _source(settings):
            if error:
                return [self._message_item("error", t("unavailable"), error_text(error))]
            return [self._message_item("loading", t("loading"), None)]
        now = self._now()
        _start, end = self._window(now)
        horizon = end if settings.range == "today_tomorrow" else end - timedelta(days=1)
        upcoming = [e for e in snapshot.events if e.end_at > now and e.start_at < horizon]
        if not upcoming:
            title = t("empty_today" if settings.range == "today" else "empty_today_tomorrow")
            note = error_text(error) if error else (t("updating") if refreshing else None)
            return [self._message_item("empty", title, note)]
        items = []
        for event in upcoming[:MAX_ITEMS]:
            actions = []
            if event.url:
                actions.append(action("open", t("open_in_calendar"),
                                      icon="internet-web-browser", kind="primary"))
            actions.append(action("refresh", t("refresh"), icon="view-refresh"))
            soon = event.start_at - now <= timedelta(minutes=15)
            items.append(card_item(
                event.key, event.title,
                icon="appointment-soon" if soon and not event.all_day else "x-office-calendar",
                subtitle=self._when(event, now), actions=actions,
            ))
        return items

    def _message_item(self, item_id: str, title: str, subtitle: str | None) -> dict[str, object]:
        return card_item(item_id, title, icon="x-office-calendar", subtitle=subtitle,
                         actions=[action("refresh", t("refresh"), icon="view-refresh")])

    def _when(self, event: Occurrence, now: datetime) -> str:
        start = event.start_at.astimezone(self._zone)
        end = event.end_at.astimezone(self._zone)
        today = now.astimezone(self._zone).date()

        def day(value: datetime) -> str:
            if value.date() == today:
                return t("today")
            if value.date() == today + timedelta(days=1):
                return t("tomorrow")
            return f"{t('weekdays').split()[value.weekday()]} {value:%d.%m.}"

        if event.all_day:
            last = (end - timedelta(seconds=1)).date() if end > start else start.date()
            if start.date() <= today <= last:
                text = t("all_day_today")
            else:
                text = t("all_day", day=day(start))
        elif start <= now:
            text = t("now_until", end=f"{end:%H:%M}") + (
                "" if end.date() == today else f" ({day(end)})"
            )
        else:
            text = f"{day(start)} {start:%H:%M}–{end:%H:%M}"
        if event.location:
            text += f" · {event.location}"
        return text

    # ---- actions (worker thread) ------------------------------------------------------

    def invoke(self, item_id: str, action_id: str, args: dict[str, object]) -> str:
        if not ID.fullmatch(item_id) or not ID.fullmatch(action_id):
            return action_reply(False, t("unknown_action"))
        if action_id == "refresh":
            try:
                self.refresh(force_feeds=True)
            except CalDavError as error:
                if error.token == "unconfigured":
                    return action_reply(False, t("not_set_up_yet"))
                self._to_main(self._card_changed)
                return action_reply(False, error_text(problem_code(error)))
            except SettingsError as error:
                return action_reply(False, str(error))
            self._to_main(self._card_changed)
            with self._lock:
                problem = self._last_error
            if problem:   # one source failed, the other worked
                return action_reply(False, error_text(problem))
            return action_reply(True, t("updated"))
        if item_id == NOTIFY_ITEM and action_id.startswith("open-"):
            return self._open(action_id[len("open-"):])
        if action_id == "open":
            return self._open(item_id)
        return action_reply(False, t("unknown_action"))

    def _open(self, key: str) -> str:
        snapshot = self._current()
        with self._lock:
            event = next((e for e in snapshot.events if e.key == key), None)
        if event is None:
            return action_reply(False, t("event_gone"))
        if not event.url:
            return action_reply(False, t("no_link"))
        return action_reply(True, None, event.url)

    # ---- status and settings ---------------------------------------------------------

    def status(self) -> dict[str, object]:
        try:
            settings = self._settings.load()
        except SettingsError as error:
            return {"state": "error", "detail": str(error)}
        if settings is None:
            return {"state": "unconfigured", "detail": t("setup_hint")}
        servers = [host_of(settings.url)] if settings.use_caldav else []
        if settings.use_ical:
            servers += settings.feed_hosts
        server = ", ".join(dict.fromkeys(h for h in servers if h))
        with self._lock:
            error, refreshing = self._last_error, self._refreshing
        if refreshing:
            return {"state": "busy", "server": server}
        if error:
            return {"state": "error", "server": server, "detail": error_text(error)}
        return {"state": "ok", "server": server}

    def config_values(self) -> dict[str, object]:
        try:
            settings = self._settings.load()
        except SettingsError as error:
            raise PluginCallError(str(error)) from None
        if settings is None:
            return {}
        stored = False
        if settings.url and settings.username:
            try:
                stored = bool(self._settings.password(settings))
            except SettingsError:
                stored = False
        return {
            "use_caldav": settings.use_caldav,
            "url": settings.url, "username": settings.username, "password": stored,
            # Never the links: GetConfig shows a stored secret as a mask.
            "use_ical": settings.use_ical, "ical_urls": bool(settings.feeds_id),
            "ical_names": ", ".join(settings.feed_names),
            "calendars": ", ".join(settings.calendars), "range": settings.range,
            "reminder": settings.reminder, "hosts": ", ".join(extra_hosts(settings)),
            "use_system_proxy": settings.use_system_proxy,
            "allow_http_feeds": settings.allow_http_feeds,
        }

    def _typed_settings(
        self, values: dict[str, object],
    ) -> tuple[Settings, str, tuple[str, ...], Settings | None]:
        """The settings a form describes, its password and iCal links (typed
        or stored) and the stored settings. Raises ConfigError for a bad field."""
        try:
            current = self._settings.load()
        except SettingsError:
            current = None
        use_caldav = values.get("use_caldav") is not False
        use_ical = values.get("use_ical") is True
        if not use_caldav and not use_ical:
            raise ConfigError("use_caldav", t("no_source"))
        raw_url = str(values.get("url") or "")
        username = str(values.get("username") or "").strip()
        password = ""
        if use_caldav:
            try:
                url = normalize_url(raw_url)
            except CalDavError:
                raise ConfigError("url", error_text("invalid-url")) from None
            if not username:
                raise ConfigError("username", t("required"))
            password = str(values.get("password") or "")
            if not password and current is not None and current.url:
                # The stored password only goes back to the server it belongs to.
                if (host_of(current.url), current.username) != (host_of(url), username):
                    raise ConfigError("password", t("password_for_server"))
                try:
                    password = self._settings.password(current)
                except SettingsError:
                    password = ""
            if not password:
                raise ConfigError("password", t("required"))
        else:
            try:   # kept for later, not used while CalDAV is off
                url = normalize_url(raw_url) if raw_url.strip() else ""
            except CalDavError:
                url = ""
        hosts = split_hosts(str(values.get("hosts") or ""))
        if any(not valid_host(host) for host in hosts):
            raise ConfigError("hosts", t("bad_hosts"))
        allow_http = values.get("allow_http_feeds") is True
        links: tuple[str, ...] = ()
        if use_ical:
            typed = str(values.get("ical_urls") or "")
            try:
                if typed.strip():
                    links = parse_links(typed, allow_http=allow_http)
                elif current is not None and current.feeds_id:
                    try:
                        stored = self._settings.feeds(current)
                    except SettingsError:
                        stored = ()
                    links = tuple(normalize_link(link, allow_http=allow_http, index=index)
                                  for index, link in enumerate(stored))
            except FeedError as error:
                raise feed_config_error(error) from None
            if not links:
                raise ConfigError("ical_urls", t("required"))
        new = Settings(
            url=url, username=username,
            calendars=split_names(str(values.get("calendars") or "")),
            range=str(values.get("range") or "today_tomorrow"),
            reminder=str(values.get("reminder") or "off"), hosts=hosts,
            use_system_proxy=values.get("use_system_proxy") is True,
            use_caldav=use_caldav, use_ical=use_ical,
            feeds_id=fingerprint(links) if links else current.feeds_id if current else "",
            feeds_store=current.feeds_store if current else "keyring",
            feed_hosts=(tuple(host_of(link) for link in links) if links
                        else current.feed_hosts if current else ()),
            feed_names=split_feed_names(str(values.get("ical_names") or "")),
            allow_http_feeds=allow_http,
        )
        return new, password, links, current

    def apply_config(self, values: dict[str, object]) -> None:
        """Worker thread. Check server, account, calendars and links; then store."""
        new, password, links, current = self._typed_settings(values)
        typed_hosts = new.hosts
        same_network = current is not None and extra_hosts(current) == typed_hosts and (
            current.use_system_proxy == new.use_system_proxy)
        login = new.use_caldav and (
            current is None or not current.use_caldav or current.url != new.url
            or current.username != new.username or "password" in values or not same_network
        )
        check_caldav = new.use_caldav and (
            login or current is None or current.calendars != new.calendars)
        check_feeds = new.use_ical and (
            current is None or not current.use_ical or "ical_urls" in values
            or current.feeds_id != new.feeds_id
            or current.allow_http_feeds != new.allow_http_feeds or not same_network
        )
        feeds: list[Feed] = []
        if check_caldav or check_feeds:
            # Entering a host under "Allowed hosts" is the confirmation; the
            # config then keeps the hosts the checks actually used.
            used: set[str] = set()
            if check_caldav:
                used |= set(self.verify(new, password)[1])
            if check_feeds:
                feeds, feed_hosts = verify_feeds(self._feed_client_factory, new, links)
                used |= feed_hosts
            if (new.use_caldav and not check_caldav) or (new.use_ical and not check_feeds):
                used |= set(typed_hosts)   # unchecked source: keep what was typed
            new = replace(new, hosts=tuple(sorted(used)))
        try:
            key_store = current.key_store if current is not None else "keyring"
            if login:
                key_store = self._settings.save_password(
                    new, password, prefer_keyring=current is None or current.key_store != "file",
                )
                if current is not None and current.key_store == "keyring" and current.url and (
                        current.url, current.username) != (new.url, new.username):
                    self._settings.forget_keyring(current)
            feeds_store = new.feeds_store
            if links and (current is None or current.feeds_id != new.feeds_id):
                feeds_store = self._settings.save_feeds(
                    links, prefer_keyring=current is None or current.feeds_store != "file",
                )
            self._settings.save_options(replace(new, key_store=key_store,
                                                feeds_store=feeds_store))
        except (SettingsError, OSError) as error:
            raise ConfigError("", t("store_failed", error=error)) from None
        log.info("calendar settings saved")
        with self._lock:
            self._last_error = ""
        if feeds:
            with self._refresh_lock:   # what the check fetched, so no second fetch
                now = self._now()
                for link, feed in zip(links, feeds, strict=True):
                    self._feeds[link] = feed
                    self._feed_times[link] = now
        if check_caldav or check_feeds or current is None or _source(current) != _source(new):
            try:
                self.refresh()
            except (CalDavError, SettingsError):
                pass
        self._to_main(self._card_changed)

    def verify(
        self, settings: Settings, password: str,
    ) -> tuple[list[CalendarInfo], tuple[str, ...]]:
        return verify(self._client_factory, settings, password)

    # ---- settings helpers (ApiVersion 1.3) --------------------------------------------

    def test_config(self, values: dict[str, object]) -> ConfigTestResult:
        """Worker thread. "Test connection": discover the calendars and load
        the iCal links; store nothing."""
        settings, password, links, _current = self._typed_settings(values)
        parts = []
        if settings.use_caldav:
            calendars, _hosts = self.verify(settings, password)
            parts.append(calendars_message(settings.username, calendars))
        if settings.use_ical:
            feeds, _used = verify_feeds(self._feed_client_factory, settings, links)
            parts.append(feeds_message(settings, links, feeds))
        log.info("calendar settings tested")
        return passed(one_line(parts))

    def config_login(self, provider: str, values: dict[str, object]) -> LoginStep:
        """Worker thread. "Sign in with Nextcloud": open Login Flow v2."""
        step = self._login.login_step(str(values.get("url") or ""))
        if step.state == "open":
            with self._lock:
                self._login_values[step.login_id] = dict(values)
                while len(self._login_values) > 4:
                    self._login_values.pop(next(iter(self._login_values)))
        return step

    def config_login_status(self, login_id: str) -> LoginStep:
        with self._lock:
            values = dict(self._login_values.get(login_id, {}))
        step = self._login.status_step(
            login_id, lambda credentials: self._store_login(credentials, values),
        )
        if step.final:
            with self._lock:
                self._login_values.pop(login_id, None)
        return step

    def config_login_cancel(self, login_id: str) -> None:
        self._login.cancel(login_id)
        with self._lock:
            self._login_values.pop(login_id, None)

    def _store_login(self, credentials: Credentials, values: dict[str, object]) -> str:
        """Nextcloud granted an app password: check the calendars, then store."""
        try:
            current = self._settings.load()
        except SettingsError:
            current = None
        hosts = split_hosts(str(values.get("hosts") or ""))
        base = current if current is not None else Settings(url="", username="")
        new = replace(
            base,   # the iCal links and their options stay as they are
            url=normalize_url(credentials.dav_url + "/"), username=credentials.login_name,
            use_caldav=True,
            calendars=split_names(str(values.get("calendars") or "")),
            range=str(values.get("range") or "today_tomorrow"),
            reminder=str(values.get("reminder") or "off"),
            hosts=tuple(h for h in hosts if valid_host(h)),
            use_system_proxy=values.get("use_system_proxy") is True,
        )
        calendars, used = self.verify(new, credentials.app_password)
        if current is not None and current.use_ical:
            used = tuple(sorted({*used, *extra_hosts(current)}))
        new = replace(new, hosts=used)
        prefer_keyring = current is None or current.key_store != "file"
        try:
            self._settings.save(new, credentials.app_password, prefer_keyring=prefer_keyring)
        except (SettingsError, OSError) as error:
            raise ConfigError("", t("store_failed", error=error)) from None
        if current is not None and current.key_store == "keyring" and current.url and (
            current.url, current.username) != (new.url, new.username):
            self._settings.forget_keyring(current)
        log.info("calendar settings saved after the Nextcloud sign-in")
        with self._lock:
            self._last_error = ""
        try:
            self.refresh()
        except (CalDavError, SettingsError):
            pass
        self._to_main(self._card_changed)
        return calendars_message(credentials.login_name, calendars)

    # ---- D-Bus: capability card ---------------------------------------------------------

    def _card_changed(self) -> None:
        self.CardChanged()

    @dbus.service.method(
        SURFACE_INTERFACE, in_signature="", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def GetCardItems(self, reply, error, sender=None) -> None:
        self.admit(sender)
        self.run_async(lambda: card_reply(self.card_items()), reply, error)

    @dbus.service.method(
        SURFACE_INTERFACE, in_signature="sss", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def InvokeAction(self, item_id, action_id, args_json, reply, error, sender=None) -> None:
        self.admit(sender)
        item, act, args = str(item_id), str(action_id), parse_args(str(args_json))
        self.run_async(lambda: self.invoke(item, act, args), reply, error)

    @dbus.service.signal(SURFACE_INTERFACE, signature="")
    def CardChanged(self) -> None:
        """Content-free: the core calls GetCardItems again."""

    # ---- D-Bus: capability notify ---------------------------------------------------------

    @dbus.service.signal(SURFACE_INTERFACE, signature="sssss")
    def Notify(self, title, body, icon, action_label, action_id) -> None:
        """A reminder; the core shows it under its notification policy."""


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None
