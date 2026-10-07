"""Server, user and choices in a config file; the password in the keyring.

The password goes to the desktop Secret Service through libsecret, keyed by
server URL and user name. Without a usable keyring it falls back to an
owner-only file next to the config. It never appears in logs, the manifest,
D-Bus replies or command lines.
"""
from __future__ import annotations

import functools
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

from blueferry_plugin_kit import secrets
from blueferry_plugin_kit.secrets import MAX_FILE_BYTES, KeyringStore, SecretsError

from blueferry_calendar import PLUGIN_ID

SCHEMA = "io.weirdware.blueferry.calendar.Password"
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

    @property
    def reminder_minutes(self) -> int:
        return int(self.reminder) if self.reminder.isdigit() else 0

    def with_store(self, key_store: str) -> Settings:
        return replace(self, key_store=key_store)


class SettingsStore(KeyringStore):
    SECRET_SCHEMA = SCHEMA
    SECRET_ATTRIBUTES = ("server", "user")
    SECRET_LABEL = "BlueFerry calendar password"

    def __init__(self, directory: Path | None = None, *, secret: Any = None) -> None:
        # secret: gi.repository.Secret, injectable for tests
        super().__init__(directory or config_dir(), secret=secret)

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
        if not isinstance(raw, dict) or not isinstance(raw.get("url"), str) or not isinstance(
            raw.get("username"), str,
        ):
            raise SettingsError("config.json has no server URL or user name")
        calendars = raw.get("calendars", [])
        if not isinstance(calendars, list):
            calendars = []
        hosts = raw.get("hosts", [])
        if not isinstance(hosts, list):
            hosts = []
        return Settings(
            url=raw["url"],
            username=raw["username"],
            key_store="file" if raw.get("key_store") == "file" else "keyring",
            calendars=tuple(str(name) for name in calendars if isinstance(name, str)),
            range=raw.get("range") if raw.get("range") in RANGES else "today_tomorrow",
            reminder=raw.get("reminder") if raw.get("reminder") in REMINDERS else "off",
            hosts=tuple(h.lower() for h in hosts if isinstance(h, str) and 0 < len(h) <= 253),
            use_system_proxy=raw.get("use_system_proxy") is True,
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
        }, indent=2) + "\n")

    @_translated
    def password(self, settings: Settings) -> str:
        return self.load_secret(
            settings.key_store, self._attributes(settings),
            missing="the password file is missing; run setup again",
            empty="no password stored; run setup again",
        )

    def forget(self) -> None:
        settings = None
        try:
            settings = self.load()
        except SettingsError:
            pass
        if settings is not None and settings.key_store == "keyring":
            self.forget_keyring(settings)
        self.key_path.unlink(missing_ok=True)
        self.config_path.unlink(missing_ok=True)

    @_translated
    def forget_keyring(self, settings: Settings) -> None:
        self.clear_keyring(self._attributes(settings))

    @staticmethod
    def _attributes(settings: Settings) -> dict[str, str]:
        return {"server": settings.url, "user": settings.username}
