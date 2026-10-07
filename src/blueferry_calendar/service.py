"""The plugin process: a CalDAV agenda as a card, reminders as notifications.

Threads: D-Bus calls and the timer run on the GLib main loop; network and
keyring work runs on worker threads (``run_async`` or ``start_worker``),
and signals are emitted back on the main loop. The agenda is fetched every
ten minutes and on request ("Refresh"); the card answers from memory or the
cache at once and refreshes in the background when the data is stale.

Nothing about an event (title, place, time, link) is ever logged.
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
from blueferry_plugin_kit.auth.nextcloud import Credentials, NextcloudLogin
from blueferry_plugin_kit.configtest import connected, passed
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
from blueferry_calendar.settings import Settings, SettingsError, SettingsStore, split_names
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

REFRESH_EVERY = timedelta(minutes=10)
RETRY_AFTER = timedelta(minutes=2)
TICK_SECONDS = 30
NOTIFIED_KEEP = timedelta(days=2)
SETUP_HINT = (
    "set the CalDAV server, user name and password in BlueFerry's settings "
    "(Plugins > Calendar), or run: blueferry plugins calendar setup --url URL --user NAME"
)
ERROR_TEXT = {
    "unauthorized": "the server refused the user name or password",
    "forbidden": "the server refused access",
    "not-found": "the server has no CalDAV service at this address",
    "server-error": "the calendar server reported an error",
    "network": "the calendar server is not reachable",
    "too-large": "the server sent more data than allowed",
    "bad-response": "the server's answer was not understood",
    "redirect": "the server redirected too often",
    "foreign-host": "the server pointed to a host that is not allowed; run setup again",
    "invalid-url": "the address must start with https:// (http only for localhost)",
    "no-principal": "no CalDAV account found at this address",
    "no-calendars": "no event calendar found for this account",
    "missing-calendars": "none of the chosen calendars exists any more",
}

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


FOREIGN_HOST = (
    "the server sends the login on to {host}; add it to 'Allowed hosts' "
    "if it belongs to your provider"
)


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
        return ConfigError("hosts", FOREIGN_HOST.format(host=error.host))
    field = "password" if error.token in ("unauthorized", "forbidden") else "url"
    return ConfigError(field, ERROR_TEXT.get(error.token, error.token))


def check_selection(calendars: list[CalendarInfo], settings: Settings) -> None:
    if not calendars:
        raise ConfigError("url", ERROR_TEXT["no-calendars"])
    _chosen, missing = select(calendars, settings.calendars)
    if missing:
        available = ", ".join(c.name for c in calendars)
        raise ConfigError(
            "calendars", f"not found: {', '.join(missing)}; available: {available}"[:300],
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
    head = f"{connected(user)}; {len(names)} calendar{'' if len(names) == 1 else 's'}: "
    text = head + ", ".join(names) + "."
    if len(text) > MAX_MESSAGE:
        text = text[:MAX_MESSAGE - 1].rstrip(", ") + "…"
    return text


def _source(settings: Settings) -> str:
    """Which settings an agenda belongs to (a hash, nothing readable)."""
    text = "\n".join([settings.url, settings.username, *settings.calendars])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class CalendarService(PluginService):
    def __init__(
        self,
        manifest: PluginManifest,
        bus: Any = None,
        *,
        settings: SettingsStore | None = None,
        cache: AgendaCache | None = None,
        client_factory: Callable[..., CalDavClient] = new_client,
        now: Callable[[], datetime] = _utc_now,
        zone: tzinfo | None = None,
        every: Every | None = _every,
        login: NextcloudLogin | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(manifest, bus, **kwargs)
        self._login = login or NextcloudLogin(user_agent="BlueFerry Calendar")
        self._login_values: dict[str, dict[str, object]] = {}
        self._settings = settings or SettingsStore()
        self._cache = cache or AgendaCache()
        self._client_factory = client_factory
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

    def _connect(self) -> tuple[Settings, CalDavClient]:
        settings = self._settings.load()
        if settings is None:
            raise CalDavError("unconfigured")
        password = self._settings.password(settings)
        return settings, connect(self._client_factory, settings, password)

    def refresh(self) -> bool:
        """Fetch the window's events; True when the agenda changed. Blocking."""
        with self._refresh_lock:
            try:
                settings, client = self._connect()
                now = self._now()
                start, end = self._window(now)
                found, missing = select(client.calendars(), settings.calendars)
                if not found:
                    raise CalDavError("missing-calendars" if missing else "no-calendars")
                events: list[Occurrence] = []
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
            except SettingsError as error:
                with self._lock:
                    self._last_error = str(error)
                raise
            except CalDavError as error:
                with self._lock:
                    self._last_error = error.token
                raise
            events.sort(key=lambda item: (item.start_at, item.title))
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
                    or self._last_error != ""
                )
                self._snapshot = snapshot
                self._last_error = ""
            self._save(snapshot)
            log.info("calendar refreshed: %d calendars, %d events", len(found), len(events))
            return changed

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
        body = f"In {minutes} min, {local:%H:%M}–{event.end_at.astimezone(self._zone):%H:%M}"
        if event.location:
            body += f" · {event.location}"
        label, action_id = ("Open", f"open-{event.key}") if event.url else ("", "")
        self.Notify(event.title, body, "appointment-soon", label, action_id)
        log.info("reminder shown")

    # ---- card ---------------------------------------------------------------------

    def card_items(self) -> list[dict[str, object]]:
        """The card: the next events of the configured range. Worker thread."""
        try:
            settings = self._settings.load()
        except SettingsError as error:
            return [self._message_item("error", "Calendar settings unreadable", str(error))]
        if settings is None:
            return [card_item(
                "setup", "Calendar not set up", icon="x-office-calendar",
                subtitle="Add your CalDAV server in BlueFerry's settings (Plugins > Calendar)",
            )]
        if self._due(settings):
            self._refresh_in_background()
        snapshot = self._current()
        with self._lock:
            error = self._last_error
            refreshing = self._refreshing
        if snapshot.source != _source(settings):
            if error:
                return [self._message_item("error", "Calendar unavailable",
                                           ERROR_TEXT.get(error, error))]
            return [self._message_item("loading", "Loading calendar…", None)]
        now = self._now()
        _start, end = self._window(now)
        horizon = end if settings.range == "today_tomorrow" else end - timedelta(days=1)
        upcoming = [e for e in snapshot.events if e.end_at > now and e.start_at < horizon]
        if not upcoming:
            title = ("No more events today" if settings.range == "today"
                     else "No more events today or tomorrow")
            note = ERROR_TEXT.get(error, error) if error else (
                "Updating…" if refreshing else None
            )
            return [self._message_item("empty", title, note)]
        items = []
        for event in upcoming[:MAX_ITEMS]:
            actions = []
            if event.url:
                actions.append(action("open", "Open in calendar",
                                      icon="internet-web-browser", kind="primary"))
            actions.append(action("refresh", "Refresh", icon="view-refresh"))
            soon = event.start_at - now <= timedelta(minutes=15)
            items.append(card_item(
                event.key, event.title,
                icon="appointment-soon" if soon and not event.all_day else "x-office-calendar",
                subtitle=self._when(event, now), actions=actions,
            ))
        return items

    def _message_item(self, item_id: str, title: str, subtitle: str | None) -> dict[str, object]:
        return card_item(item_id, title, icon="x-office-calendar", subtitle=subtitle,
                         actions=[action("refresh", "Refresh", icon="view-refresh")])

    def _when(self, event: Occurrence, now: datetime) -> str:
        start = event.start_at.astimezone(self._zone)
        end = event.end_at.astimezone(self._zone)
        today = now.astimezone(self._zone).date()

        def day(value: datetime) -> str:
            if value.date() == today:
                return "Today"
            if value.date() == today + timedelta(days=1):
                return "Tomorrow"
            return f"{value:%a %d.%m.}"

        if event.all_day:
            last = (end - timedelta(seconds=1)).date() if end > start else start.date()
            if start.date() <= today <= last:
                text = "Today, all day"
            else:
                text = f"{day(start)}, all day"
        elif start <= now:
            text = f"Now until {end:%H:%M}" + (
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
            return action_reply(False, "Unknown action")
        if action_id == "refresh":
            try:
                self.refresh()
            except CalDavError as error:
                if error.token == "unconfigured":
                    return action_reply(False, "The calendar is not set up yet")
                self._to_main(self._card_changed)
                return action_reply(False, ERROR_TEXT.get(error.token, error.token))
            except SettingsError as error:
                return action_reply(False, str(error))
            self._to_main(self._card_changed)
            return action_reply(True, "Calendar updated")
        if item_id == NOTIFY_ITEM and action_id.startswith("open-"):
            return self._open(action_id[len("open-"):])
        if action_id == "open":
            return self._open(item_id)
        return action_reply(False, "Unknown action")

    def _open(self, key: str) -> str:
        snapshot = self._current()
        with self._lock:
            event = next((e for e in snapshot.events if e.key == key), None)
        if event is None:
            return action_reply(False, "This event is no longer in the agenda")
        if not event.url:
            return action_reply(False, "This event has no link")
        return action_reply(True, None, event.url)

    # ---- status and settings ---------------------------------------------------------

    def status(self) -> dict[str, object]:
        try:
            settings = self._settings.load()
        except SettingsError as error:
            return {"state": "error", "detail": str(error)}
        if settings is None:
            return {"state": "unconfigured", "detail": SETUP_HINT}
        server = settings.url.split("://", 1)[-1].split("/", 1)[0]
        with self._lock:
            error, refreshing = self._last_error, self._refreshing
        if refreshing:
            return {"state": "busy", "server": server}
        if error:
            return {"state": "error", "server": server, "detail": ERROR_TEXT.get(error, error)}
        return {"state": "ok", "server": server}

    def config_values(self) -> dict[str, object]:
        try:
            settings = self._settings.load()
        except SettingsError as error:
            raise PluginCallError(str(error)) from None
        if settings is None:
            return {}
        try:
            stored = bool(self._settings.password(settings))
        except SettingsError:
            stored = False
        return {
            "url": settings.url, "username": settings.username, "password": stored,
            "calendars": ", ".join(settings.calendars), "range": settings.range,
            "reminder": settings.reminder, "hosts": ", ".join(extra_hosts(settings)),
            "use_system_proxy": settings.use_system_proxy,
        }

    def _typed_settings(
        self, values: dict[str, object],
    ) -> tuple[Settings, str, Settings | None]:
        """The settings a form describes, its password (typed or stored) and
        the stored settings. Raises ConfigError for a bad field."""
        try:
            url = normalize_url(str(values.get("url") or ""))
        except CalDavError:
            raise ConfigError("url", ERROR_TEXT["invalid-url"]) from None
        username = str(values.get("username") or "").strip()
        if not username:
            raise ConfigError("username", "is required")
        try:
            current = self._settings.load()
        except SettingsError:
            current = None
        password = str(values.get("password") or "")
        if not password and current is not None:
            # The stored password only goes back to the server it belongs to.
            if (host_of(current.url), current.username) != (host_of(url), username):
                raise ConfigError("password", "enter the password for this server and user")
            try:
                password = self._settings.password(current)
            except SettingsError:
                password = ""
        if not password:
            raise ConfigError("password", "is required")
        hosts = split_hosts(str(values.get("hosts") or ""))
        if any(not valid_host(host) for host in hosts):
            raise ConfigError("hosts", "must be host names separated by commas")
        new = Settings(
            url=url, username=username, calendars=split_names(str(values.get("calendars") or "")),
            range=str(values.get("range") or "today_tomorrow"),
            reminder=str(values.get("reminder") or "off"), hosts=hosts,
            use_system_proxy=values.get("use_system_proxy") is True,
        )
        return new, password, current

    def apply_config(self, values: dict[str, object]) -> None:
        """Worker thread. Check server, account and calendars; then store."""
        new, password, current = self._typed_settings(values)
        url, username, hosts = new.url, new.username, new.hosts
        connection = (
            current is None or current.url != url or current.username != username
            or "password" in values or extra_hosts(current) != hosts
            or current.use_system_proxy != new.use_system_proxy
        )
        if connection or current is None or current.calendars != new.calendars:
            # Entering a host under "Allowed hosts" is the confirmation; the
            # config then keeps the hosts discovery actually used.
            _calendars, used = self.verify(new, password)
            new = replace(new, hosts=used)
        try:
            if connection:
                prefer_keyring = current is None or current.key_store != "file"
                self._settings.save(new, password, prefer_keyring=prefer_keyring)
                if current is not None and current.key_store == "keyring" and (
                    current.url, current.username) != (url, username):
                    self._settings.forget_keyring(current)
            else:
                self._settings.save_options(new.with_store(current.key_store))
        except (SettingsError, OSError) as error:
            raise ConfigError("", f"could not store the settings: {error}") from None
        log.info("calendar settings saved")
        with self._lock:
            self._last_error = ""
        if connection or current is None or current.calendars != new.calendars:
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
        """Worker thread. "Test connection": discover the calendars, store nothing."""
        settings, password, _current = self._typed_settings(values)
        calendars, _hosts = self.verify(settings, password)
        log.info("calendar settings tested")
        return passed(calendars_message(settings.username, calendars))

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
        new = Settings(
            url=normalize_url(credentials.dav_url + "/"), username=credentials.login_name,
            calendars=split_names(str(values.get("calendars") or "")),
            range=str(values.get("range") or "today_tomorrow"),
            reminder=str(values.get("reminder") or "off"),
            hosts=tuple(h for h in hosts if valid_host(h)),
            use_system_proxy=values.get("use_system_proxy") is True,
        )
        calendars, used = self.verify(new, credentials.app_password)
        new = replace(new, hosts=used)
        prefer_keyring = current is None or current.key_store != "file"
        try:
            self._settings.save(new, credentials.app_password, prefer_keyring=prefer_keyring)
        except (SettingsError, OSError) as error:
            raise ConfigError("", f"could not store the settings: {error}") from None
        if current is not None and current.key_store == "keyring" and (
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
