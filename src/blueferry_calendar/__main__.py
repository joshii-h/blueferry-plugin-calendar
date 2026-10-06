"""``blueferry-calendar serve|setup|calendars|agenda|status|forget``."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from blueferry.plugin_api.config import ConfigError
from blueferry.plugin_api.manifest import ManifestError, default_directories
from blueferry.plugin_api.service import run

from blueferry_calendar import PLUGIN_ID, manifest_text
from blueferry_calendar.agenda import local_zone
from blueferry_calendar.caldav import CalDavClient, CalDavError, normalize_url
from blueferry_calendar.service import ERROR_TEXT, CalendarService, select, verify, window
from blueferry_calendar.settings import (
    RANGES,
    REMINDERS,
    Settings,
    SettingsError,
    SettingsStore,
    split_names,
)
from blueferry_calendar.surfaces import load_manifest

ENTRY_POINT = "blueferry-calendar"


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _command() -> list[str]:
    """How the bus and BlueFerry should start this plugin."""
    beside = Path(sys.executable).parent / ENTRY_POINT
    if beside.is_file() and os.access(beside, os.X_OK):
        return [str(beside)]
    installed = shutil.which(ENTRY_POINT)
    if installed:
        return [installed]
    import blueferry.plugin_api as api

    roots = [
        str(Path(__file__).resolve().parents[1]),
        str(Path(api.__file__).resolve().parents[2]),
    ]
    return [
        "/usr/bin/env", "PYTHONPATH=" + ":".join(dict.fromkeys(roots)),
        sys.executable, "-m", "blueferry_calendar",
    ]


def install_activation(data_home: Path | None = None) -> list[Path]:
    """Write the user manifest and D-Bus service file unless the system has them."""
    data_home = data_home or _data_home()
    template = load_manifest(manifest_text())
    written: list[Path] = []
    system = [d for d in default_directories() if not str(d).startswith(str(data_home))]
    if not any((directory / f"{PLUGIN_ID}.plugin").exists() for directory in system):
        command = _command()
        text = manifest_text().replace(
            f"Exec={ENTRY_POINT} serve", "Exec=" + shlex.join([*command, "serve"]),
        ).replace(f"Cli={ENTRY_POINT}", "Cli=" + shlex.join(command))
        load_manifest(text)  # never install something clients would ignore
        target = data_home / "blueferry" / "plugins" / f"{PLUGIN_ID}.plugin"
        _write(target, text)
        written.append(target)
        service = data_home / "dbus-1" / "services" / f"{template.bus_name}.service"
        _write(service, "[D-BUS Service]\nName={}\nExec={}\n".format(
            template.bus_name, shlex.join([*command, "serve"]),
        ))
        written.append(service)
    return written


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, path)


def _error(error: CalDavError) -> str:
    return ERROR_TEXT.get(error.token, error.token)


def setup(args: argparse.Namespace, store: SettingsStore | None = None,
          client_factory=CalDavClient) -> int:
    store = store or SettingsStore()
    try:
        url = normalize_url(args.url)
    except CalDavError:
        print("The URL must start with https:// (http only for localhost).", file=sys.stderr)
        return 2
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        print("For iCloud use an app-specific password (account.apple.com >")
        print("Sign-In and Security > App-Specific Passwords).")
        password = getpass.getpass("CalDAV password (input hidden): ")
    if not password or len(password) > 4096:
        print("No password given.", file=sys.stderr)
        return 2
    settings = Settings(
        url=url, username=args.user.strip(), calendars=split_names(args.calendars or ""),
        range=args.range, reminder=args.reminder,
    )
    if not args.no_verify:
        try:
            calendars = verify(client_factory, settings, password)
        except ConfigError as error:
            print(f"Check failed ({error.field or 'settings'}): {error.message}.", file=sys.stderr)
            return 1
        print("Calendars: " + ", ".join(c.name for c in select(calendars, settings.calendars)[0]))
    try:
        where = store.save(settings, password, prefer_keyring=not args.key_file)
    except (SettingsError, OSError) as error:
        print(f"Could not store the settings: {error}", file=sys.stderr)
        return 1
    print("Password stored in the desktop keyring." if where == "keyring"
          else f"Password stored in {store.key_path} (owner-only).")
    for path in install_activation():
        print(f"Installed {path}")
    print("Done. BlueFerry's phone card shows the agenda.")
    return 0


def _connect(store: SettingsStore, client_factory=CalDavClient) -> tuple[Settings, CalDavClient]:
    settings = store.load()
    if settings is None:
        raise SettingsError("not configured; run: blueferry-calendar setup --url URL --user NAME")
    return settings, client_factory(settings.url, settings.username, store.password(settings))


def list_calendars(store: SettingsStore | None = None, client_factory=CalDavClient) -> int:
    try:
        settings, client = _connect(store or SettingsStore(), client_factory)
        calendars = client.calendars()
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 1
    except CalDavError as error:
        print(f"Discovery failed: {_error(error)}.", file=sys.stderr)
        return 1
    chosen = {c.url for c in select(calendars, settings.calendars)[0]}
    for calendar in calendars:
        print(("* " if calendar.url in chosen else "  ") + calendar.name)
    print("(* = shown; choose with: blueferry plugins config "
          f"{PLUGIN_ID} --set calendars='Name, Other')")
    return 0


def agenda(store: SettingsStore | None = None, client_factory=CalDavClient) -> int:
    """Print today's and tomorrow's events (to the terminal, never a log)."""
    from blueferry_calendar.agenda import occurrences

    zone = local_zone()
    try:
        settings, client = _connect(store or SettingsStore(), client_factory)
        found, _missing = select(client.calendars(), settings.calendars)
        now = datetime.now(timezone.utc)
        start, end = window(now, zone)
        events = []
        for calendar in found:
            events += occurrences(client.events(calendar.url, start, end), start, end, zone,
                                  calendar=calendar.name, calendar_id=calendar.url)
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 1
    except CalDavError as error:
        print(f"Failed: {_error(error)}.", file=sys.stderr)
        return 1
    for event in sorted(events, key=lambda e: e.start_at):
        when = "all day" if event.all_day else f"{event.start_at.astimezone(zone):%a %H:%M}"
        place = f"  @ {event.location}" if event.location else ""
        print(f"{when:>10}  {event.title}{place}  [{event.calendar}]")
    if not events:
        print("No events today or tomorrow.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=ENTRY_POINT, description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve on the session bus (started by D-Bus)")
    set_up = commands.add_parser("setup", help="store the server, user and password")
    set_up.add_argument("--url", required=True,
                        help="e.g. https://caldav.icloud.com or https://cloud.example.org")
    set_up.add_argument("--user", required=True)
    set_up.add_argument("--calendars", default="", help="comma-separated names; empty: all")
    set_up.add_argument("--range", choices=RANGES, default="today_tomorrow")
    set_up.add_argument("--reminder", choices=REMINDERS, default="off",
                        help="minutes before an event, or off")
    set_up.add_argument("--key-file", action="store_true",
                        help="store the password in an owner-only file instead of the keyring")
    set_up.add_argument("--password-stdin", action="store_true",
                        help="read the password from standard input (for scripts)")
    set_up.add_argument("--no-verify", action="store_true",
                        help="do not test the account against the server")
    commands.add_parser("calendars", help="list the account's calendars")
    commands.add_parser("agenda", help="print today's and tomorrow's events")
    commands.add_parser("status", help="show whether the plugin is configured")
    commands.add_parser("forget", help="remove the stored password and settings")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "setup":
        return setup(args)
    if args.command == "calendars":
        return list_calendars()
    if args.command == "agenda":
        return agenda()
    if args.command == "forget":
        SettingsStore().forget()
        from blueferry_calendar.cache import AgendaCache

        AgendaCache().clear()
        print("Removed the stored CalDAV settings, password and cached agenda.")
        return 0
    if args.command == "status":
        try:
            settings = SettingsStore().load()
        except SettingsError as error:
            print(error)
            return 1
        print(f"Configured for {settings.username} at {settings.url}" if settings
              else "Not configured.")
        return 0
    try:
        manifest = load_manifest(manifest_text())
    except ManifestError as error:
        print(error, file=sys.stderr)
        return 1
    return run(lambda bus: CalendarService(manifest, bus).start())


if __name__ == "__main__":
    sys.exit(main())
