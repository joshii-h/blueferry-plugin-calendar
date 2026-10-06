# blueferry-plugin-calendar

Today's and tomorrow's events from a CalDAV calendar in BlueFerry.

A plugin for [BlueFerry](https://github.com/joshii-h/blueferry) that shows
your next events in the "From plugins" section of the phone card (capability
`card`) and, if you like, reminds you 10 or 15 minutes before an event starts
(capability `notify`). It works with any CalDAV server: iCloud, Nextcloud,
Radicale, Baikal and others. It runs as its own process on the session bus and
talks to BlueFerry only through `blueferry.plugin_api` (plugin API 1.2, see
`PLUGINS.md` in the BlueFerry repository).

## Install

```sh
blueferry plugins install https://github.com/joshii-h/blueferry-plugin-calendar
```

or open BlueFerry's settings, Plugins, and pick "Calendar". BlueFerry shows
the source, the version tag and commit, the capabilities and the command it
will run, and installs only after you confirm. Update with
`blueferry plugins update io.weirdware.blueferry.calendar`, remove with
`blueferry plugins remove io.weirdware.blueferry.calendar`.

The plugin needs a BlueFerry whose plugin API knows the `card` and `notify`
capabilities (1.2); older versions ignore its manifest.

## Configure

Open BlueFerry's settings, Plugins > Calendar > Settings, or use the command
line (the password is asked for, never passed as an argument):

```sh
blueferry plugins config io.weirdware.blueferry.calendar \
    --set url=https://caldav.icloud.com --set username=you@icloud.com --secret password
```

| Setting | Meaning |
| --- | --- |
| `url` | The CalDAV server; `https://` (plain `http://` only for localhost). The plugin finds your calendars itself. |
| `username` | Your account name. |
| `password` | Your password or app password; checked against the server before it is stored. |
| `calendars` | Comma-separated calendar names to show; empty shows all event calendars. |
| `range` | `today` or `today_tomorrow` (default). |
| `reminder` | `off` (default), `10` or `15`: a desktop notification that many minutes before an event. |

`blueferry plugins calendar calendars` lists the calendars of your account
(`*` marks the ones shown); `blueferry plugins calendar agenda` prints today's
and tomorrow's events in the terminal.

### iCloud

1. Create an app-specific password: [account.apple.com](https://account.apple.com)
   > Sign-In and Security > App-Specific Passwords. Your normal Apple Account
   password does not work for CalDAV.
2. Server `https://caldav.icloud.com`, user name your Apple Account e-mail
   address, password the app-specific password.

iCloud keeps your calendars on a partition host (`pNN-caldav.icloud.com`); the
plugin follows it there. Reminders lists are not calendars and are skipped.

### Nextcloud

Server `https://cloud.example.org` (your Nextcloud address; the plugin finds
`/remote.php/dav` through `/.well-known/caldav`). If you use two-factor
authentication, create an app password under Personal settings > Security >
Devices & sessions.

### Generic CalDAV (Radicale, Baikal, …)

Enter the server's address, for example `https://dav.example.org/` for
Radicale or `https://dav.example.org/dav.php` for Baikal. Discovery follows
RFC 6764 and RFC 4791: `current-user-principal`, then `calendar-home-set`,
first at the address you gave, then at `/.well-known/caldav`, then at `/`.
Basic and Digest authentication are supported (Baikal uses Digest by default).

## How it works

- The agenda is fetched every 10 minutes and when you press "Refresh" on the
  card. Network requests run off the main loop with a 15-second timeout each;
  after an error the plugin waits two minutes before the next try.
- Recurring events (RRULE, RDATE, EXDATE, moved instances) are expanded
  locally; times with a time zone are shown in your local zone, floating times
  as local time, all-day events as "all day".
- The card shows at most 8 events. "Open in calendar" appears when the event
  carries an `http(s)` link (for example a meeting URL).
- Reminders carry the title, time and place; BlueFerry decides under its
  notification policy whether the content is shown. With reminders on, the
  plugin keeps running instead of exiting when idle.
- The password goes to the desktop keyring (Secret Service); without one, to
  an owner-only file. It never appears in logs, the manifest, D-Bus replies
  or command lines. Credentials are only sent over https (http only to
  localhost) and only to the server you configured (the same site, so a
  redirect or a calendar link cannot carry them elsewhere).
- The last agenda is cached in `~/.cache/blueferry/calendar/agenda.json`
  (directory 0700, file 0600, not encrypted), so the card shows something
  right after a restart and reminders are not repeated.
- Logs contain counts and error codes only, never titles, places, times or
  links.

`blueferry-calendar forget` removes the settings, the password and the cache.

## Develop

```sh
python3 -m venv --system-site-packages .venv   # dbus-python, PyGObject, libsecret from the system
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

`blueferry-plugin-api` comes from the `plugin-api` directory of the BlueFerry
repository. The tests replay CalDAV responses modelled on iCloud, Nextcloud,
Radicale and Baikal (`tests/fixtures/`) and drive the plugin through a fake
BlueFerry host that checks every reply against the v1.2 surface spec. The
plugin has not been tested against live accounts yet.

## License

GPL-2.0-or-later, like BlueFerry. See `LICENSE`.
