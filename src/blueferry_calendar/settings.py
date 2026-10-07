"""Server, user and choices in a config file; password and iCal links in the keyring.

The CalDAV password goes to the desktop Secret Service through libsecret,
keyed by server URL and user name; the iCal subscription links (each one a
secret: whoever has it reads the calendar) are a second keyring entry.
Without a usable keyring each falls back to an owner-only file next to the
config (``password``, ``feeds``). Neither ever appears in logs, the
manifest, D-Bus replies or command lines; the config holds only a hash of
the links and their hosts.
"""
from __future__ import annotations

import functools
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from blueferry_plugin_kit import secrets
from blueferry_plugin_kit.secrets import MAX_FILE_BYTES, KeyringStore, SecretsError

from blueferry_calendar import PLUGIN_ID

SCHEMA = "io.weirdware.blueferry.calendar.Password"
FEEDS_SCHEMA = "io.weirdware.blueferry.calendar.Feeds"
RANGES = ("today", "today_tomorrow")
REMINDERS = ("off", "10", "15")

_T = TypeVar("_T")


class SettingsError(SecretsError):
    """The settings, password or cache file cannot be used.

    A subclass of the kit's :class:`SecretsError`: everything in this plugin
    raises (and logs) SettingsError, and ``except SecretsError`` still works.
    """


def _translated(function: Callable[..., _T]) -> Callable[..., _T]:
    """Re-raise the kit's SecretsError as SettingsError, same message."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return function(*args, **kwargs)
        except SettingsError:
            raise
        except SecretsError as error:
            raise SettingsError(str(error)) from None

    return wrapper


read_private = _translated(secrets.read_private_text)
write_private = _translated(secrets.write_private)

__all__ = [
    "FEEDS_SCHEMA",
    "MAX_FILE_BYTES",
    "RANGES",
    "REMINDERS",
    "SCHEMA",
    "Settings",
    "SettingsError",
    "SettingsStore",
    "config_dir",
    "read_private",
    "split_names",
    "write_private",
]


def config_dir() -> Path:
    return secrets.config_dir(PLUGIN_ID)


def split_names(value: str) -> tuple[str, ...]:
    """``"Work, Home"`` -> ``("Work", "Home")``; empty means every calendar."""
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


@dataclass(frozen=True, slots=True)
class Settings:
    url: str
    username: str
    key_store: str = "keyring"          # "keyring" or "file"
    calendars: tuple[str, ...] = ()     # display names (or URLs); empty: all
    range: str = "today_tomorrow"
    reminder: str = "off"
    # Hosts the login may go to, confirmed at setup (iCloud's partition
    # host, say). Missing in older configs: then only the URL's own host.
    hosts: tuple[str, ...] = ()
    # Off: connect directly, ignoring http(s)_proxy from the environment.
    use_system_proxy: bool = False
    # Sources: a CalDAV account, iCal subscription links, or both.
    use_caldav: bool = True
    use_ical: bool = False
    feeds_id: str = ""                  # hash of the stored links; "" = none
    feeds_store: str = "keyring"        # "keyring" or "file"
    feed_hosts: tuple[str, ...] = ()    # the links' hosts, in order (for messages)
    feed_names: tuple[str, ...] = ()    # optional names, in the links' order
    allow_http_feeds: bool = False

    @property
    def reminder_minutes(self) -> int:
        return int(self.reminder) if self.reminder.isdigit() else 0

    def with_store(self, key_store: str) -> Settings:
        return replace(self, key_store=key_store)


class FeedSecrets(KeyringStore):
    """The iCal links: one keyring entry, fallback file ``feeds``."""

    SECRET_SCHEMA = FEEDS_SCHEMA
    SECRET_ATTRIBUTES = ("kind",)
    SECRET_LABEL = "BlueFerry calendar iCal links"
    ATTRIBUTES: ClassVar[dict[str, str]] = {"kind": "ical-links"}

    @property
    def key_path(self) -> Path:
        return self.directory / "feeds"


class SettingsStore(KeyringStore):
    SECRET_SCHEMA = SCHEMA
    SECRET_ATTRIBUTES = ("server", "user")
    SECRET_LABEL = "BlueFerry calendar password"

    def __init__(self, directory: Path | None = None, *, secret: Any = None) -> None:
        # secret: gi.repository.Secret, injectable for tests
        super().__init__(directory or config_dir(), secret=secret)
        self.feed_secrets = FeedSecrets(self.directory, secret=secret)

    @property
    def config_path(self) -> Path:
        return self.directory / "config.json"

    @property
    def key_path(self) -> Path:
        return self.directory / "password"

    @_translated
    def load(self) -> Settings | None:
        try:
            raw = json.loads(read_private(self.config_path))
        except FileNotFoundError:
            return None
        except ValueError:
            raise SettingsError("config.json is not valid JSON") from None
        if not isinstance(raw, dict):
            raise SettingsError("config.json has no server URL or user name")
        use_caldav = raw.get("use_caldav") is not False
        if use_caldav and (not isinstance(raw.get("url"), str) or not isinstance(
            raw.get("username"), str,
        )):
            raise SettingsError("config.json has no server URL or user name")
        calendars = raw.get("calendars", [])
        if not isinstance(calendars, list):
            calendars = []
        hosts = raw.get("hosts", [])
        if not isinstance(hosts, list):
            hosts = []
        return Settings(
            url=raw["url"] if isinstance(raw.get("url"), str) else "",
            username=raw["username"] if isinstance(raw.get("username"), str) else "",
            key_store="file" if raw.get("key_store") == "file" else "keyring",
            calendars=tuple(str(name) for name in calendars if isinstance(name, str)),
            range=raw.get("range") if raw.get("range") in RANGES else "today_tomorrow",
            reminder=raw.get("reminder") if raw.get("reminder") in REMINDERS else "off",
            hosts=tuple(h.lower() for h in hosts if isinstance(h, str) and 0 < len(h) <= 253),
            use_system_proxy=raw.get("use_system_proxy") is True,
            use_caldav=use_caldav,
            use_ical=raw.get("use_ical") is True,
            feeds_id=str(raw.get("feeds_id") or "")[:64],
            feeds_store="file" if raw.get("feeds_store") == "file" else "keyring",
            feed_hosts=_strings(raw.get("feed_hosts"), 253),
            feed_names=_strings(raw.get("feed_names"), 200),
            allow_http_feeds=raw.get("allow_http_feeds") is True,
        )

    @_translated
    def save(self, settings: Settings, password: str, *, prefer_keyring: bool = True) -> str:
        """Store the password (keyring first) and the config; return the store."""
        store = self.save_secret(
            self._attributes(settings), password, prefer_keyring=prefer_keyring,
        )
        self.save_options(settings.with_store(store))
        return store

    @_translated
    def save_options(self, settings: Settings) -> None:
        """Rewrite the config only; the password stays where it is."""
        write_private(self.config_path, json.dumps({
            "url": settings.url, "username": settings.username,
            "key_store": settings.key_store, "calendars": list(settings.calendars),
            "range": settings.range, "reminder": settings.reminder,
            "hosts": list(settings.hosts), "use_system_proxy": settings.use_system_proxy,
            "use_caldav": settings.use_caldav, "use_ical": settings.use_ical,
            "feeds_id": settings.feeds_id, "feeds_store": settings.feeds_store,
            "feed_hosts": list(settings.feed_hosts), "feed_names": list(settings.feed_names),
            "allow_http_feeds": settings.allow_http_feeds,
        }, indent=2) + "\n")

    @_translated
    def password(self, settings: Settings) -> str:
        return self.load_secret(
            settings.key_store, self._attributes(settings),
            missing="the password file is missing; run setup again",
            empty="no password stored; run setup again",
        )

    @_translated
    def save_feeds(self, links: tuple[str, ...], *, prefer_keyring: bool = True) -> str:
        """Store the iCal links (keyring first); return the store."""
        return self.feed_secrets.save_secret(
            FeedSecrets.ATTRIBUTES, "\n".join(links), prefer_keyring=prefer_keyring,
        )

    @_translated
    def feeds(self, settings: Settings) -> tuple[str, ...]:
        """The stored iCal links (empty when none are configured)."""
        if not settings.feeds_id:
            return ()
        text = self.feed_secrets.load_secret(
            settings.feeds_store, FeedSecrets.ATTRIBUTES,
            missing="the file with the iCal links is missing; enter them again",
            empty="no iCal links stored; enter them again",
        )
        return tuple(line.strip() for line in text.splitlines() if line.strip())

    def forget(self) -> None:
        settings = None
        try:
            settings = self.load()
        except SettingsError:
            pass
        if settings is not None and settings.key_store == "keyring" and settings.url:
            self.forget_keyring(settings)
        self.feed_secrets.clear_keyring(FeedSecrets.ATTRIBUTES)
        self.key_path.unlink(missing_ok=True)
        self.feed_secrets.key_path.unlink(missing_ok=True)
        self.config_path.unlink(missing_ok=True)

    @_translated
    def forget_keyring(self, settings: Settings) -> None:
        self.clear_keyring(self._attributes(settings))

    @staticmethod
    def _attributes(settings: Settings) -> dict[str, str]:
        return {"server": settings.url, "user": settings.username}


def _strings(value: object, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item[:limit] for item in value if isinstance(item, str))[:16]
