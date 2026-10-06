"""Test doubles: a CalDAV server from fixtures, a keyring and a v1.2 host.

``FakeServer`` replays ``fixtures/<server>/routes.json``: ``"METHOD URL"``
maps to a status, headers and a body file. The bodies are modelled on what
iCloud, Nextcloud, Radicale and Baikal send (namespace prefixes, absolute vs.
relative hrefs, extra collections, auth challenges); they were written from
the servers' documented behaviour, not captured from live accounts.
``{{ics:NAME}}`` in a body is replaced by ``fixtures/ics/NAME`` (XML-escaped),
``{{raw:NAME}}`` by the raw text (for CDATA sections).

``FakeHost`` plays the BlueFerry core of PLUGIN-SURFACES-v1.2: it calls
``GetCardItems``/``InvokeAction`` like the core would, checks every reply
against the spec, and records the content-free ``CardChanged`` and the
``Notify`` signals.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
import urllib.parse
from pathlib import Path
from xml.sax.saxutils import escape

from blueferry.plugin_api.manifest import parse_manifest

from blueferry_calendar import manifest_text
from blueferry_calendar.caldav import Response

FIXTURES = Path(__file__).parent / "fixtures"
USER, PASSWORD = "alice", "abcd-efgh-ijkl-mnop"


def plugin_manifest():
    """The shipped manifest, parsed by the real parser.

    blueferry-plugin-api before 1.2 drops the unknown capabilities ``card``
    and ``notify``; parse with a known one and put them back, so the rest of
    the manifest (settings schema, ids, commands) is still checked for real.
    """
    text = manifest_text()
    try:
        return parse_manifest(text)
    except ValueError:
        swapped = text.replace("Capabilities=card;notify;", "Capabilities=photos;")
        return dataclasses.replace(
            parse_manifest(swapped), capabilities=("card", "notify"), api_minor=2,
        )


class FakeServer:
    def __init__(self, name: str, *, password: str = PASSWORD) -> None:
        self.directory = FIXTURES / name
        config = json.loads((self.directory / "routes.json").read_text())
        self.auth = config["auth"]
        self.challenge = config["challenge"]
        self.routes = dict(config["routes"])
        self.password = password
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        self.overrides: dict[str, Response] = {}

    def body(self, name: str) -> bytes:
        text = (self.directory / name).read_text()

        def ics(match: re.Match) -> str:
            raw = (FIXTURES / "ics" / match.group(2)).read_text()
            return raw if match.group(1) == "raw" else escape(raw)

        return re.sub(r"\{\{(ics|raw):([\w.-]+)\}\}", ics, text).encode()

    def _authorized(self, method: str, url: str, header: str) -> bool:
        if self.auth == "basic":
            expected = base64.b64encode(f"{USER}:{self.password}".encode()).decode()
            return header == f"Basic {expected}"
        if not header.startswith("Digest "):
            return False
        fields = dict(
            (key, quoted or raw) for key, quoted, raw in
            re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', header[7:])
        )
        realm = re.search(r'realm="([^"]*)"', self.challenge).group(1)
        nonce = re.search(r'nonce="([^"]*)"', self.challenge).group(1)
        uri = urllib.parse.urlsplit(url).path

        def h(text: str) -> str:
            return hashlib.md5(text.encode()).hexdigest()

        ha1 = h(f"{USER}:{realm}:{self.password}")
        ha2 = h(f"{method}:{uri}")
        expected = h(f"{ha1}:{nonce}:{fields.get('nc')}:{fields.get('cnonce')}:auth:{ha2}")
        return (fields.get("username") == USER and fields.get("uri") == uri
                and fields.get("nonce") == nonce and fields.get("response") == expected
                and fields.get("opaque") is not None)

    def __call__(self, method, url, headers, body, timeout) -> Response:
        assert timeout <= 30
        self.requests.append((method, url, dict(headers), body))
        if not self._authorized(method, url, headers.get("Authorization", "")):
            return Response(401, {"www-authenticate": self.challenge}, b"", url)
        key = f"{method} {url}"
        if key in self.overrides:
            return self.overrides[key]
        route = self.routes.get(key)
        if route is None:
            return Response(404, {}, b"", url)
        if method == "REPORT":
            assert headers.get("Depth") == "1"
            assert b"<c:time-range start=" in body and b'name="VEVENT"' in body
        if method == "PROPFIND":
            assert headers.get("Depth") in ("0", "1")
        reply_headers = {k.lower(): v for k, v in route.get("headers", {}).items()}
        data = self.body(route["body"]) if "body" in route else b""
        return Response(route["status"], reply_headers, data, url)

    def urls(self, method: str | None = None) -> list[str]:
        return [url for m, url, *_ in self.requests if method in (None, m)]


class FakeSecret:
    """Enough of gi.repository.Secret for SettingsStore."""

    COLLECTION_DEFAULT = "default"

    class SchemaFlags:
        NONE = 0

    class SchemaAttributeType:
        STRING = "string"

    class Schema:
        @staticmethod
        def new(name, flags, attributes):
            return name

    def __init__(self, *, fail: bool = False) -> None:
        self.items: dict[tuple, str] = {}
        self.fail = fail

    def password_store_sync(self, schema, attributes, collection, label, password, cancel):
        if self.fail:
            raise RuntimeError("no keyring")
        self.items[(schema, tuple(sorted(attributes.items())))] = password
        return True

    def password_lookup_sync(self, schema, attributes, cancel):
        return self.items.get((schema, tuple(sorted(attributes.items()))))

    def password_clear_sync(self, schema, attributes, cancel):
        return self.items.pop((schema, tuple(sorted(attributes.items()))), None) is not None


# ---- the core's side of PLUGIN-SURFACES-v1.2 ----------------------------------------

_ICON = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SpecViolation(AssertionError):
    pass


def _check(condition: bool, what: str) -> None:
    if not condition:
        raise SpecViolation(what)


def check_card_reply(text: str) -> list[dict]:
    reply = json.loads(text)
    _check(isinstance(reply, dict) and set(reply) == {"items"}, "reply is {items: [...]}")
    items = reply["items"]
    _check(isinstance(items, list) and len(items) <= 8, "at most 8 items")
    ids = set()
    for item in items:
        _check(set(item) == {"id", "icon", "title", "subtitle", "actions"}, f"item keys {item}")
        _check(isinstance(item["id"], str) and _ID.fullmatch(item["id"]), "item id")
        _check(item["id"] not in ids, "item ids unique")
        ids.add(item["id"])
        _check(isinstance(item["icon"], str) and _ICON.fullmatch(item["icon"]), "icon name")
        _check(isinstance(item["title"], str) and 0 < len(item["title"]) <= 80, "title")
        _check(item["subtitle"] is None or (
            isinstance(item["subtitle"], str) and len(item["subtitle"]) <= 160), "subtitle")
        _check(isinstance(item["actions"], list) and len(item["actions"]) <= 3, "≤3 actions")
        for act in item["actions"]:
            _check(set(act) == {"id", "label", "icon", "kind"}, "action keys")
            _check(isinstance(act["id"], str) and _ID.fullmatch(act["id"]), "action id")
            _check(isinstance(act["label"], str) and 0 < len(act["label"]) <= 40, "label")
            _check(act["icon"] is None or _ICON.fullmatch(act["icon"]), "action icon")
            _check(act["kind"] in ("button", "primary"), "action kind")
    return items


def check_action_reply(text: str, cache_dir: Path) -> dict:
    reply = json.loads(text)
    _check(isinstance(reply, dict) and set(reply) == {"ok", "message", "open_uri"},
           "reply is {ok, message, open_uri}")
    _check(isinstance(reply["ok"], bool), "ok is a bool")
    _check(reply["message"] is None or isinstance(reply["message"], str), "message")
    uri = reply["open_uri"]
    if uri is not None:
        parts = urllib.parse.urlsplit(uri)
        if parts.scheme == "file":
            _check(Path(parts.path).resolve().is_relative_to(cache_dir.resolve()),
                   "file:// only below the plugin cache")
        else:
            _check(parts.scheme in ("http", "https") and bool(parts.hostname),
                   "open_uri is http(s)")
    return reply


class FakeHost:
    def __init__(self, service, cache_dir: Path) -> None:
        self.service = service
        self.cache_dir = cache_dir
        self.card_changed = 0
        self.notifications: list[tuple[str, str, str, str, str]] = []
        service.CardChanged = self._on_card_changed
        service.Notify = self._on_notify

    def _on_card_changed(self, *args) -> None:
        _check(args == (), "CardChanged carries no content")
        self.card_changed += 1

    def _on_notify(self, *args) -> None:
        _check(len(args) == 5 and all(isinstance(a, str) for a in args), "Notify(sssss)")
        title, _body, icon, label, action_id = args
        _check(bool(title) and _ICON.fullmatch(icon) is not None, "notify title and icon")
        _check(bool(label) == bool(action_id), "action label and id come together")
        self.notifications.append(args)

    def _call(self, method, *args):
        outcome: dict = {}
        getattr(self.service, method)(
            *args, reply=lambda value: outcome.setdefault("reply", value),
            error=lambda failure: outcome.setdefault("error", failure), sender=":1.host",
        )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["reply"]

    def card(self) -> list[dict]:
        return check_card_reply(self._call("GetCardItems"))

    def invoke(self, item_id: str, action_id: str, args: str = "{}") -> dict:
        return check_action_reply(self._call("InvokeAction", item_id, action_id, args),
                                  self.cache_dir)

    def click_notification(self, index: int = -1) -> dict:
        action_id = self.notifications[index][4]
        return self.invoke("notify", action_id, "{}")
