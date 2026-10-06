"""Server, user and choices in a config file; the password in the keyring.

The password goes to the desktop Secret Service through libsecret, keyed by
server URL and user name. Without a usable keyring it falls back to an
owner-only file next to the config. It never appears in logs, the manifest,
D-Bus replies or command lines.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from blueferry_calendar import PLUGIN_ID

SCHEMA = "io.weirdware.blueferry.calendar.Password"
MAX_FILE_BYTES = 16 * 1024
RANGES = ("today", "today_tomorrow")
REMINDERS = ("off", "10", "15")


class SettingsError(Exception):
    pass


def config_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(config_home) / "blueferry" / "plugins" / PLUGIN_ID


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

    @property
    def reminder_minutes(self) -> int:
        return int(self.reminder) if self.reminder.isdigit() else 0

    def with_store(self, key_store: str) -> Settings:
        return replace(self, key_store=key_store)


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SettingsError("config directory has the wrong owner or type")
    path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> None:
    _private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def read_private(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SettingsError(f"{path.name} has the wrong owner or type")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SettingsError(f"{path.name} is readable by other users")
        text = stream.read(limit + 1)
    if len(text) > limit:
        raise SettingsError(f"{path.name} is too large")
    return text


class SettingsStore:
    def __init__(self, directory: Path | None = None, *, secret: Any = None) -> None:
        self.directory = directory or config_dir()
        self._secret = secret  # gi.repository.Secret, injectable for tests

    @property
    def config_path(self) -> Path:
        return self.directory / "config.json"

    @property
    def key_path(self) -> Path:
        return self.directory / "password"

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
        return Settings(
            url=raw["url"],
            username=raw["username"],
            key_store="file" if raw.get("key_store") == "file" else "keyring",
            calendars=tuple(str(name) for name in calendars if isinstance(name, str)),
            range=raw.get("range") if raw.get("range") in RANGES else "today_tomorrow",
            reminder=raw.get("reminder") if raw.get("reminder") in REMINDERS else "off",
        )

    def save(self, settings: Settings, password: str, *, prefer_keyring: bool = True) -> str:
        """Store the password (keyring first) and the config; return the store."""
        store = "file"
        if prefer_keyring and self._store_keyring(settings, password):
            store = "keyring"
            self.key_path.unlink(missing_ok=True)
        else:
            write_private(self.key_path, password + "\n")
        self.save_options(settings.with_store(store))
        return store

    def save_options(self, settings: Settings) -> None:
        """Rewrite the config only; the password stays where it is."""
        write_private(self.config_path, json.dumps({
            "url": settings.url, "username": settings.username,
            "key_store": settings.key_store, "calendars": list(settings.calendars),
            "range": settings.range, "reminder": settings.reminder,
        }, indent=2) + "\n")

    def password(self, settings: Settings) -> str:
        if settings.key_store == "file":
            try:
                value = read_private(self.key_path).rstrip("\n")
            except FileNotFoundError:
                raise SettingsError("the password file is missing; run setup again") from None
        else:
            value = self._lookup_keyring(settings)
        if not value:
            raise SettingsError("no password stored; run setup again")
        return value

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

    def forget_keyring(self, settings: Settings) -> None:
        secret = self._module()
        if secret is None:
            return
        try:
            secret.password_clear_sync(self._schema(secret), self._attributes(settings), None)
        except Exception:  # nosec B110 - best effort
            pass

    # ---- libsecret -------------------------------------------------------------

    @staticmethod
    def _attributes(settings: Settings) -> dict[str, str]:
        return {"server": settings.url, "user": settings.username}

    def _module(self) -> Any:
        if self._secret is not None:
            return self._secret
        try:
            import gi

            gi.require_version("Secret", "1")
            from gi.repository import Secret
        except (ImportError, ValueError):
            return None
        self._secret = Secret
        return Secret

    @staticmethod
    def _schema(secret: Any) -> Any:
        return secret.Schema.new(SCHEMA, secret.SchemaFlags.NONE, {
            "server": secret.SchemaAttributeType.STRING,
            "user": secret.SchemaAttributeType.STRING,
        })

    def _store_keyring(self, settings: Settings, password: str) -> bool:
        secret = self._module()
        if secret is None:
            return False
        try:
            return bool(secret.password_store_sync(
                self._schema(secret), self._attributes(settings), secret.COLLECTION_DEFAULT,
                "BlueFerry calendar password", password, None,
            ))
        except Exception:
            return False

    def _lookup_keyring(self, settings: Settings) -> str:
        secret = self._module()
        if secret is None:
            raise SettingsError("no Secret Service client is installed")
        try:
            value = secret.password_lookup_sync(
                self._schema(secret), self._attributes(settings), None,
            )
        except Exception:
            raise SettingsError("the desktop keyring is locked or unavailable") from None
        return str(value or "")
