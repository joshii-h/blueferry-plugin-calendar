"""User-visible strings in German and English, picked from the locale
(the same scheme as blueferry-plugin-localsend and -webdav)."""
from __future__ import annotations

import os

_DE = {
    "setup_hint": (
        "CalDAV-Server, Benutzername und Passwort in den BlueFerry-Einstellungen "
        "eintragen (Plugins > Kalender) oder ausführen: "
        "blueferry plugins calendar setup --url URL --user NAME"
    ),
    "err_unauthorized": "der Server hat Benutzername oder Passwort abgelehnt",
    "err_forbidden": "der Server verweigert den Zugriff",
    "err_not-found": "unter dieser Adresse gibt es keinen CalDAV-Dienst",
    "err_server-error": "der Kalenderserver meldet einen Fehler",
    "err_network": "der Kalenderserver ist nicht erreichbar",
    "err_too-large": "der Server hat mehr Daten geschickt als erlaubt",
    "err_bad-response": "Antwort des Servers nicht verstanden",
    "err_redirect": "der Server leitet zu oft um",
    "err_foreign-host": (
        "der Server verweist auf einen nicht erlaubten Host; Einrichtung wiederholen"
    ),
    "err_invalid-url": "die Adresse muss mit https:// beginnen (http nur für localhost)",
    "err_no-principal": "unter dieser Adresse wurde kein CalDAV-Konto gefunden",
    "err_no-calendars": "für dieses Konto wurde kein Terminkalender gefunden",
    "err_missing-calendars": "keiner der gewählten Kalender existiert noch",
    "foreign_host": (
        "der Server leitet die Anmeldung an {host} weiter; unter „Erlaubte Hosts“ "
        "eintragen, wenn er zu deinem Anbieter gehört"
    ),
    "not_found": "nicht gefunden: {missing}; vorhanden: {available}",
    "connected": "Verbunden als {user}",
    "calendars_one": "{count} Kalender",
    "calendars_many": "{count} Kalender",
    "required": "ist erforderlich",
    "password_for_server": "Passwort für diesen Server und Benutzer eingeben",
    "bad_hosts": "Hostnamen durch Kommas getrennt eintragen",
    "store_failed": "Einstellungen konnten nicht gespeichert werden: {error}",
    "login_insecure": "Die Anmeldung braucht eine https://-Serveradresse.",
    "login_invalid-url": "Serveradresse eintragen, z. B. https://cloud.example.org.",
    "login_unreachable": "Der Server ist nicht erreichbar.",
    "login_not-nextcloud": "Dieser Server bietet die Nextcloud-Anmeldung nicht an.",
    "login_redirect": "Der Server leitet um; die Adresse eintragen, bei der du landest.",
    "login_refused": "Der Server hat die Anmeldung abgelehnt.",
    "login_bad-reply": "Die Antwort des Servers ist keine Nextcloud-Anmeldung.",
    "login_expired": "Die Anmeldung hat zu lange gedauert; bitte neu starten.",
    "login_unknown": "Diese Anmeldung läuft nicht mehr; bitte neu starten.",
    "login_cancelled": "Anmeldung abgebrochen.",
    "login_store-failed": "Angemeldet, aber das App-Passwort konnte nicht gespeichert werden.",
    "reminder": "In {minutes} Min., {start}–{end}",
    "open": "Öffnen",
    "settings_unreadable": "Kalendereinstellungen nicht lesbar",
    "not_set_up": "Kalender nicht eingerichtet",
    "not_set_up_hint": (
        "CalDAV-Server in den BlueFerry-Einstellungen eintragen (Plugins > Kalender)"
    ),
    "unavailable": "Kalender nicht verfügbar",
    "loading": "Kalender wird geladen …",
    "empty_today": "Heute keine Termine mehr",
    "empty_today_tomorrow": "Heute und morgen keine Termine mehr",
    "updating": "Wird aktualisiert …",
    "open_in_calendar": "Im Kalender öffnen",
    "refresh": "Aktualisieren",
    "today": "Heute",
    "tomorrow": "Morgen",
    "weekdays": "Mo Di Mi Do Fr Sa So",
    "all_day_today": "Heute, ganztägig",
    "all_day": "{day}, ganztägig",
    "now_until": "Jetzt bis {end}",
    "unknown_action": "Unbekannte Aktion",
    "not_set_up_yet": "Der Kalender ist noch nicht eingerichtet",
    "updated": "Kalender aktualisiert",
    "event_gone": "Dieser Termin ist nicht mehr in der Agenda",
    "no_link": "Dieser Termin hat keinen Link",
}

_EN = {
    "setup_hint": (
        "set the CalDAV server, user name and password in BlueFerry's settings "
        "(Plugins > Calendar), or run: blueferry plugins calendar setup --url URL --user NAME"
    ),
    "err_unauthorized": "the server refused the user name or password",
    "err_forbidden": "the server refused access",
    "err_not-found": "the server has no CalDAV service at this address",
    "err_server-error": "the calendar server reported an error",
    "err_network": "the calendar server is not reachable",
    "err_too-large": "the server sent more data than allowed",
    "err_bad-response": "the server's answer was not understood",
    "err_redirect": "the server redirected too often",
    "err_foreign-host": "the server pointed to a host that is not allowed; run setup again",
    "err_invalid-url": "the address must start with https:// (http only for localhost)",
    "err_no-principal": "no CalDAV account found at this address",
    "err_no-calendars": "no event calendar found for this account",
    "err_missing-calendars": "none of the chosen calendars exists any more",
    "foreign_host": (
        "the server sends the login on to {host}; add it to 'Allowed hosts' "
        "if it belongs to your provider"
    ),
    "not_found": "not found: {missing}; available: {available}",
    "connected": "Connected as {user}",
    "calendars_one": "{count} calendar",
    "calendars_many": "{count} calendars",
    "required": "is required",
    "password_for_server": "enter the password for this server and user",
    "bad_hosts": "must be host names separated by commas",
    "store_failed": "could not store the settings: {error}",
    "login_insecure": "Sign-in needs an https:// server address.",
    "login_invalid-url": "Enter the server address, e.g. https://cloud.example.org.",
    "login_unreachable": "The server could not be reached.",
    "login_not-nextcloud": "This server does not offer the Nextcloud sign-in.",
    "login_redirect": "The server redirects; enter the address you end up at.",
    "login_refused": "The server refused the sign-in.",
    "login_bad-reply": "The server sent an answer that is not a Nextcloud sign-in.",
    "login_expired": "The sign-in took too long; start it again.",
    "login_unknown": "This sign-in is no longer running; start it again.",
    "login_cancelled": "Sign-in cancelled.",
    "login_store-failed": "Signed in, but the app password could not be stored.",
    "reminder": "In {minutes} min, {start}–{end}",
    "open": "Open",
    "settings_unreadable": "Calendar settings unreadable",
    "not_set_up": "Calendar not set up",
    "not_set_up_hint": "Add your CalDAV server in BlueFerry's settings (Plugins > Calendar)",
    "unavailable": "Calendar unavailable",
    "loading": "Loading calendar…",
    "empty_today": "No more events today",
    "empty_today_tomorrow": "No more events today or tomorrow",
    "updating": "Updating…",
    "open_in_calendar": "Open in calendar",
    "refresh": "Refresh",
    "today": "Today",
    "tomorrow": "Tomorrow",
    "weekdays": "Mon Tue Wed Thu Fri Sat Sun",
    "all_day_today": "Today, all day",
    "all_day": "{day}, all day",
    "now_until": "Now until {end}",
    "unknown_action": "Unknown action",
    "not_set_up_yet": "The calendar is not set up yet",
    "updated": "Calendar updated",
    "event_gone": "This event is no longer in the agenda",
    "no_link": "This event has no link",
}


def german() -> bool:
    for variable in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        value = os.environ.get(variable)
        if value:
            return value.split(":")[0].lower().startswith("de")
    return False


def t(key: str, **values: object) -> str:
    table = _DE if german() else _EN
    return table.get(key, _EN.get(key, key)).format(**values)
