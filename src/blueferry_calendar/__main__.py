"""``blueferry-calendar serve|setup|calendars|agenda|status|forget``.

``setup`` stores a CalDAV account; iCal links (Google Calendar and others)
are entered in BlueFerry's settings form, so they never appear on a
command line or in the shell history.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import shlex
import shutil
import sys
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from blueferry.plugin_api.config import ConfigError
from blueferry.plugin_api.manifest import ManifestError, default_directories
from blueferry.plugin_api.service import run
from blueferry_plugin_kit.dav.caldav import CalDavClient, CalDavError, normalize_url, valid_host
from blueferry_plugin_kit.dav.ical import local_zone

from blueferry_calendar import PLUGIN_ID, manifest_text
from blueferry_calendar.feeds import feed_id
from blueferry_calendar.service import (
    CalendarService,
    check_selection,
    config_error,
    connect,
    discover,
    error_text,
    feed_name,
    new_client,
    new_feed_client,
    select,
    verify_feeds,
    window,
)
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
    return error_text(error.token)


def setup(args: argparse.Namespace, store: SettingsStore | None = None,
          client_factory=new_client) -> int:
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
    allowed = tuple(h.strip().lower() for h in getattr(args, "allow_host", None) or ())
    if any(not valid_host(host) for host in allowed):
        print("--allow-host takes a host name, e.g. p42-caldav.icloud.com.", file=sys.stderr)
        return 2
    try:
        current = store.load()
    except SettingsError:
        current = None
    # The iCal links and their options stay as they are.
    settings = replace(
        current if current is not None else Settings(url="", username=""),
        url=url, username=args.user.strip(), calendars=split_names(args.calendars or ""),
        range=args.range, reminder=args.reminder, hosts=allowed, use_caldav=True,
    )
    if not args.no_verify:
        interactive = not args.password_stdin and sys.stdin.isatty()
        while True:
            try:
                calendars, used = discover(client_factory, settings, password)
                check_selection(calendars, settings)
                break
            except CalDavError as error:
                if error.token == "foreign-host" and error.host:
                    # Nothing was sent to that host yet; ask before it is.
                    if interactive and _confirm(error.host):
                        settings = replace(settings, hosts=(*settings.hosts, error.host))
                        continue
                    print(f"The server sends the login on to {error.host}. If that host "
                          "belongs to your provider, run setup again with "
                          f"--allow-host {error.host}.", file=sys.stderr)
                    return 1
                problem = config_error(error)
            except ConfigError as error:
                problem = error
            print(f"Check failed ({problem.field or 'settings'}): {problem.message}.",
                  file=sys.stderr)
            return 1
        # The hosts discovery used become the allowlist in the config.
        settings = replace(settings, hosts=used)
        print("Calendars: " + ", ".join(c.name for c in select(calendars, settings.calendars)[0]))
        extra = [h for h in used if h != urllib.parse.urlsplit(url).hostname]
        if extra:
            print("Allowed hosts: " + ", ".join(extra))
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


def _confirm(host: str) -> bool:
    print(f"The server sends the login on to {host}.")
    answer = input(f"Allow {host} to receive the user name and password? [y/N] ")
    return answer.strip().lower() in ("y", "yes", "j", "ja")


def _connect(store: SettingsStore, client_factory=new_client) -> tuple[Settings, CalDavClient]:
    settings = store.load()
    if settings is None:
        raise SettingsError("not configured; run: blueferry-calendar setup --url URL --user NAME")
    if not settings.use_caldav:
        raise SettingsError("no CalDAV account is set up (only iCal links)")
    return settings, connect(client_factory, settings, store.password(settings))


def list_calendars(store: SettingsStore | None = None, client_factory=new_client) -> int:
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


def agenda(store: SettingsStore | None = None, client_factory=new_client,
           feed_client_factory=new_feed_client) -> int:
    """Print today's and tomorrow's events (to the terminal, never a log)."""
    from blueferry_plugin_kit.dav.ical import occurrences

    zone = local_zone()
    store = store or SettingsStore()
    events = []
    try:
        settings = store.load()
        if settings is None:
            raise SettingsError(
                "not configured; run: blueferry-calendar setup --url URL --user NAME")
        now = datetime.now(timezone.utc)
        start, end = window(now, zone)
        if settings.use_caldav:
            settings, client = _connect(store, client_factory)
            found, _missing = select(client.calendars(), settings.calendars)
            for calendar in found:
                events += occurrences(client.events(calendar.url, start, end), start, end,
                                      zone, calendar=calendar.name, calendar_id=calendar.url)
        if settings.use_ical:
            links = store.feeds(settings)
            feeds, _used = verify_feeds(feed_client_factory, settings, links)
            for index, (link, feed) in enumerate(zip(links, feeds, strict=True)):
                events += occurrences([feed.text], start, end, zone,
                                      calendar=feed_name(settings, index, feed, link),
                                      calendar_id=feed_id(link))
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 1
    except CalDavError as error:
        print(f"Failed: {_error(error)}.", file=sys.stderr)
        return 1
    except ConfigError as error:
        print(f"Failed: {error.message}.", file=sys.stderr)
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
    set_up.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                        help="another host the login may go to (e.g. iCloud's "
                             "pNN-caldav.icloud.com); asked interactively otherwise")
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
        print("Removed the stored settings, password, iCal links and cached agenda.")
        return 0
    if args.command == "status":
        try:
            settings = SettingsStore().load()
        except SettingsError as error:
            print(error)
            return 1
        if settings is None:
            print("Not configured.")
            return 0
        if settings.use_caldav:
            print(f"CalDAV: {settings.username} at {settings.url}")
        if settings.use_ical:
            hosts = ", ".join(dict.fromkeys(settings.feed_hosts)) or "none stored"
            print(f"iCal links: {len(settings.feed_hosts)} ({hosts})")
        return 0
    try:
        manifest = load_manifest(manifest_text())
    except ManifestError as error:
        print(error, file=sys.stderr)
        return 1
    return run(lambda bus: CalendarService(manifest, bus).start())


if __name__ == "__main__":
    sys.exit(main())
