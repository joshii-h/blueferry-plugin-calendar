"""The last fetched agenda below ``$XDG_CACHE_HOME/blueferry/calendar``.

One owner-only JSON file (directory 0700, file 0600): the occurrences of
the fetch window, when they were fetched, and which reminders were already
shown, so a restart neither loses the card nor repeats a reminder. Not
encrypted; it holds the same titles the card shows.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from blueferry_plugin_kit.dav.ical import Occurrence

from blueferry_calendar.settings import SettingsError, read_private, write_private

MAX_CACHE_BYTES = 2 * 1024 * 1024
VERSION = 1


def default_root() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(cache_home) / "blueferry" / "calendar"


@dataclass(slots=True)
class Snapshot:
    fetched_at: datetime | None = None
    source: str = ""                    # which settings produced it (hash, no content)
    events: list[Occurrence] = field(default_factory=list)
    notified: dict[str, str] = field(default_factory=dict)   # key -> start (ISO)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("cache directory has the wrong owner or type")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)


class AgendaCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_root()

    @property
    def path(self) -> Path:
        return self.root / "agenda.json"

    def load(self) -> Snapshot:
        try:
            raw = json.loads(read_private(self.path, MAX_CACHE_BYTES))
        except (FileNotFoundError, ValueError, SettingsError, OSError):
            return Snapshot()
        if not isinstance(raw, dict) or raw.get("version") != VERSION:
            return Snapshot()
        try:
            fetched = datetime.fromisoformat(str(raw.get("fetched_at")))
        except ValueError:
            fetched = None
        events = [item for item in map(Occurrence.from_json, raw.get("events") or [])
                  if item is not None]
        notified = raw.get("notified") if isinstance(raw.get("notified"), dict) else {}
        return Snapshot(
            fetched_at=fetched if fetched and fetched.tzinfo else None,
            source=str(raw.get("source", "")),
            events=events,
            notified={str(k): str(v) for k, v in notified.items()},
        )

    def save(self, snapshot: Snapshot) -> None:
        _private_dir(self.root.parent)
        _private_dir(self.root)
        write_private(self.path, json.dumps({
            "version": VERSION,
            "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
            "source": snapshot.source,
            "events": [item.to_json() for item in snapshot.events],
            "notified": snapshot.notified,
        }))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
