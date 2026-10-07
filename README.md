# blueferry-plugin-calendar

Today's and tomorrow's events from a CalDAV calendar or an iCal link
(Google Calendar, Outlook.com, iCloud public calendars, holidays …) in
BlueFerry.

A plugin for [BlueFerry](https://github.com/joshii-h/blueferry) that shows
your next events in the "From plugins" section of the phone card (capability
`card`) and, if you like, reminds you 10 or 15 minutes before an event starts
(capability `notify`). It works with any CalDAV server (iCloud, Nextcloud,
Radicale, Baikal and others) and, without any sign-in, with every calendar
that offers an iCal subscription link, Google Calendar included; with both
at once if you like. It runs as its own process on the session bus and
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
| `use_caldav` | On (default): use a CalDAV account. Off when you only use iCal links. |
| `use_ical` | Off (default): on to use iCal subscription links, alone or together with CalDAV. |
| `ical_urls` | The iCal links (secret), separated by spaces; `webcal://` is read as `https://`. Kept in the keyring like a password, never shown again. |
| `ical_names` | Optional names, comma-separated in the order of the links; empty uses the name the calendar sends (`X-WR-CALNAME`), else the host. |
| `allow_http_feeds` | Off (default): links must be `https://` (http only for localhost). On: plain `http://` links work too, unencrypted. |
| `url` | The CalDAV server; `https://` (plain `http://` only for localhost). The plugin finds your calendars itself. |
| `username` | Your account name (filled in by the Nextcloud sign-in). |
| `password` | Your app password; checked against the server before it is stored. A stored password is only sent to the server and user it was stored for. |
| `hosts` | Other hosts that may receive the login or an iCal request, comma-separated. Empty: only the hosts you entered. The check names a host when the server points elsewhere (iCloud: `pNN-caldav.icloud.com`). |
| `use_system_proxy` | Off (default): connect directly and ignore `http(s)_proxy` from the environment. On: requests, including the login, go through that proxy. |
| `calendars` | Comma-separated CalDAV calendar names to show; empty shows all event calendars. |
| `range` | `today` or `today_tomorrow` (default). |
| `reminder` | `off` (default), `10` or `15`: a desktop notification that many minutes before an event. |

The form groups these into CalDAV account, iCal subscriptions, Options and
Advanced (`hosts`, `use_system_proxy`, `allow_http_feeds`, folded); fields
of a source that is off are hidden. **Test connection** checks the typed
values without storing them and answers, e.g., "Connected as anna; 3
calendars: Private, Work, Family." or "iCal: 743 events found, calendar
“Private”." (calendar names only in that answer, never in a log);
from a shell: `blueferry plugins config io.weirdware.blueferry.calendar
--set … --test`.

`blueferry plugins calendar calendars` lists the calendars of your account
(`*` marks the ones shown); `blueferry plugins calendar agenda` prints today's
and tomorrow's events in the terminal.

### Google Calendar (iCal link, no sign-in)

Google's CalDAV needs OAuth; the plugin uses Google's secret iCal address
instead, which needs no sign-in but is **read-only**:

1. In Google Calendar on the web: Settings (gear) > Settings > under
   "Settings for my calendars" the calendar > **Integrate calendar** >
   **Secret address in iCal format** (German: "Privatadresse im
   iCal-Format"); copy it. [Google's help page](https://support.google.com/calendar/answer/37648).
2. BlueFerry settings > Plugins > Calendar > Settings: section "iCal
   subscriptions", switch on "Use iCal links", paste the address into "iCal
   links" (several calendars: several links separated by spaces), optionally
   a name per link. Switch off "Use a CalDAV account" if you have none.
3. Press **Test connection**, then save.

Treat the address like a password: whoever has it reads the calendar. If it
leaked, reset it on the same Google page (the old one stops working) and
paste the new one. Google refreshes these feeds with a delay, often several
hours; a change in Google Calendar shows up here only after that. The
plugin asks at most every 15 minutes (with ETag/Last-Modified, so an
unchanged feed costs a short "not modified").

The same works for Outlook.com (Settings > Calendar > Shared calendars >
Publish a calendar > ICS link), iCloud calendars shared as public
(`webcal://pNN-caldav.icloud.com/published/…`), holiday and school
calendars: paste their `https://` or `webcal://` link.

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
  card; iCal links at most every 15 minutes (and on "Refresh"), each answer
  revalidated with `If-None-Match`/`If-Modified-Since` and kept in memory
  only. With CalDAV and iCal links together, one source that fails does not
  hide the other: its error shows in the plugin's status and on "Refresh",
  and a link that fails keeps its last copy. Network requests run off the main loop with a 15-second timeout each;
  after an error the plugin waits two minutes before the next try.
- Recurring events (RRULE incl. BYSETPOS, RDATE, EXDATE, moved instances
  via RECURRENCE-ID) are expanded locally; times with a time zone (VTIMEZONE
  and TZID, also Outlook's Windows zone names, and Google's `X-WR-TIMEZONE`)
  are shown in your local zone, floating times as local time, all-day events
  as "all day". Events from iCal links show and remind exactly like CalDAV
  events.
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
- iCal links follow the same rules: https only (http to localhost, or with
  `allow_http_feeds`), the link's own host plus the allowed hosts, at most
  five redirects (each checked before it is followed), a 15-second timeout,
  8 MB per answer (also after gzip), no proxy unless `use_system_proxy` is
  on. The links are stored in the keyring (fallback: an owner-only file
  `feeds`), the config holds only a hash and their host names. Errors and
  logs name a link by position and host ("iCal link 2
  (calendar.google.com)"), never by its address.
- Server answers are parsed with `defusedxml`; a `DOCTYPE` anywhere in the
  XML is refused.
- The last agenda is cached in `~/.cache/blueferry/calendar/agenda.json`
  (directory 0700, file 0600, not encrypted), so the card shows something
  right after a restart and reminders are not repeated.
- Logs contain counts and error codes only, never titles, places, times or
  links.

`blueferry-calendar forget` removes the settings, the password, the iCal
links and the cache. iCal links are only entered in the settings form (or
`blueferry plugins config … --secret ical_urls`), never as a command-line
argument.

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
server; iCal links run over https against a fake feed server with a
synthetic Google-like calendar (`tests/fixtures/feeds/`: Windows time zone,
weekly rule with EXDATE and a moved instance, BYSETPOS, all-day, cancelled
event), and the tests check that the secret link never reaches a log, a
reply, the config or the cache. The plugin has not been tested against
live accounts yet.

## License

GPL-2.0-or-later, like BlueFerry. See `LICENSE`.
