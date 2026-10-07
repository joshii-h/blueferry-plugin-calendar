# blueferry-plugin-calendar

Today's and tomorrow's events from a CalDAV calendar in BlueFerry.

A plugin for [BlueFerry](https://github.com/joshii-h/blueferry) that shows
your next events in the "From plugins" section of the phone card (capability
`card`) and, if you like, reminds you 10 or 15 minutes before an event starts
(capability `notify`). It works with any CalDAV server: iCloud, Nextcloud,
Radicale, Baikal and others. It runs as its own process on the session bus and
talks to BlueFerry only through `blueferry.plugin_api` (plugin API 1.3, see
`PLUGINS.md` in the BlueFerry repository).

Card, reminders, action replies, status and settings messages are German or
English, following the locale (`LC_ALL`, `LC_MESSAGES`, `LANGUAGE`, `LANG`),
as in the WebDAV and LocalSend plugins; the command line stays English.

## Install

```sh
blueferry plugins install https://github.com/joshii-h/blueferry-plugin-calendar
```

or open BlueFerry's settings, Plugins, and pick "Calendar". BlueFerry shows
the source, the version tag and commit, the capabilities and the command it
will run, and installs only after you confirm. Update with
`blueferry plugins update io.weirdware.blueferry.calendar`, remove with
`blueferry plugins remove io.weirdware.blueferry.calendar`.

The plugin needs a BlueFerry with plugin API 1.3 (guided settings form,
"Test connection", "Sign in with Nextcloud"); older versions ignore its
manifest. Version 0.1.2 is the last release for plugin API 1.2.

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
| `username` | Your account name (filled in by the Nextcloud sign-in). |
| `password` | Your app password; checked against the server before it is stored. A stored password is only sent to the server and user it was stored for. |
| `hosts` | Other hosts that may receive the login, comma-separated. Empty: only the server's own host. The check names a host when the server points elsewhere (iCloud: `pNN-caldav.icloud.com`). |
| `use_system_proxy` | Off (default): connect directly and ignore `http(s)_proxy` from the environment. On: requests, including the login, go through that proxy. |
| `calendars` | Comma-separated calendar names to show; empty shows all event calendars. |
| `range` | `today` or `today_tomorrow` (default). |
| `reminder` | `off` (default), `10` or `15`: a desktop notification that many minutes before an event. |

The form groups these into Account, Options and Advanced (`hosts`,
`use_system_proxy`, folded). **Test connection** checks the typed values
without storing them and answers, e.g., "Connected as anna; 3 calendars:
Private, Work, Family." (calendar names only in that answer, never in a log);
from a shell: `blueferry plugins config io.weirdware.blueferry.calendar
--set … --test`.

`blueferry plugins calendar calendars` lists the calendars of your account
(`*` marks the ones shown); `blueferry plugins calendar agenda` prints today's
and tomorrow's events in the terminal.

### iCloud

1. Create an app-specific password: [account.apple.com](https://account.apple.com)
   > Sign-In and Security > App-Specific Passwords. Your normal Apple Account
   password does not work for CalDAV.
2. Server `https://caldav.icloud.com`, user name your Apple Account e-mail
   address, password the app-specific password.

iCloud keeps your calendars on a partition host (`pNN-caldav.icloud.com`).
The plugin sends your login there only after you allowed that host: `setup`
asks (or takes `--allow-host pNN-caldav.icloud.com`), the settings form names
it under "Allowed hosts". Reminders lists are not calendars and are skipped.

### Nextcloud

Easiest: type your Nextcloud address (`https://cloud.example.org`) and press
**Sign in with Nextcloud** (shell: `--set url=… --login`). Nextcloud opens in
the browser; after you grant access the plugin receives your login name and
a new app password ("BlueFerry Calendar" under Settings > Security > Devices
& sessions, revocable there), stores the password in the keyring, uses
`<server>/remote.php/dav/` as CalDAV address and checks the calendars. The
app password never passes through BlueFerry or a log. The sign-in only uses
https and expires after 20 minutes.

By hand: server `https://cloud.example.org` (the plugin finds
`/remote.php/dav` through `/.well-known/caldav`), your user name and an app
password created under Personal settings > Security > Devices & sessions.

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
  localhost) and only to hosts on an allowlist: the server's own host and
  the hosts you confirmed at setup. Setup stores the hosts discovery used
  (principal, calendar home, calendars) in the config. A redirect or a
  calendar link to any other host stops before a request goes there.
  Configs from 0.1.0 have no list; then only the exact host of the URL is
  allowed (run setup again for iCloud).
- Server answers are parsed with `defusedxml`; a `DOCTYPE` anywhere in the
  XML is refused.
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
repository. The CalDAV client, the event expansion and the keyring store come
from [blueferry-plugin-kit](https://github.com/joshii-h/blueferry-plugin-kit),
which tests them itself (also against Baikal). The tests here replay CalDAV
responses modelled on iCloud, Nextcloud and Radicale (`tests/fixtures/`) and
drive the plugin through the kit's fake BlueFerry host, which checks every
reply against the plugin API spec (card, notify, TestConfig, ConfigLogin).
The Nextcloud sign-in runs over https against the kit's fake Login Flow v2
server. The plugin has not been tested against
live accounts yet.

## License

GPL-2.0-or-later, like BlueFerry. See `LICENSE`.
