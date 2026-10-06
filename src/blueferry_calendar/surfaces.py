"""Plugin1 v1.2 surfaces (capabilities ``card`` and ``notify``), plugin side.

Written against the shared spec PLUGIN-SURFACES-v1.2. Interface names and
limits come from ``blueferry.plugin_api`` when it already ships them
(blueferry-plugin-api 1.2); the fallbacks below follow the naming of the
existing capability interfaces (``Photos1``).
"""
from __future__ import annotations

import json
import re

import blueferry.plugin_api as api
from blueferry.plugin_api.manifest import ManifestError, PluginManifest, parse_manifest

CARD_INTERFACE: str = getattr(api, "CARD_INTERFACE", "io.weirdware.BlueFerry.Card1")
NOTIFY_INTERFACE: str = getattr(api, "NOTIFY_INTERFACE", "io.weirdware.BlueFerry.Notify1")
CAPABILITY_CARD = "card"
CAPABILITY_NOTIFY = "notify"

MAX_ITEMS = 8
MAX_ACTIONS = 3
MAX_TITLE = 80
MAX_SUBTITLE = 160
MAX_LABEL = 40
NOTIFY_ITEM = "notify"   # item id the core uses when a notification action is clicked
ACTION_KINDS = frozenset({"button", "primary"})
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_ARGS_BYTES = 16 * 1024


def clip(text: str, limit: int) -> str:
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def action(action_id: str, label: str, *, icon: str | None = None,
           kind: str = "button") -> dict[str, object]:
    if not ID.fullmatch(action_id) or kind not in ACTION_KINDS:
        raise ValueError("invalid action")
    return {"id": action_id, "label": clip(label, MAX_LABEL), "icon": icon, "kind": kind}


def card_item(item_id: str, title: str, *, icon: str, subtitle: str | None = None,
              actions: list[dict[str, object]] | None = None) -> dict[str, object]:
    if not ID.fullmatch(item_id):
        raise ValueError("invalid item id")
    return {
        "id": item_id, "icon": icon, "title": clip(title, MAX_TITLE),
        "subtitle": clip(subtitle, MAX_SUBTITLE) if subtitle else None,
        "actions": (actions or [])[:MAX_ACTIONS],
    }


def card_reply(items: list[dict[str, object]]) -> str:
    return json.dumps({"items": items[:MAX_ITEMS]})


def action_reply(ok: bool, message: str | None = None, open_uri: str | None = None) -> str:
    return json.dumps({"ok": ok, "message": message, "open_uri": open_uri})


def parse_args(text: str) -> dict[str, object]:
    """InvokeAction's ``args_json``: a small JSON object (anything else: ``{}``)."""
    if len(text.encode("utf-8", "surrogatepass")) > MAX_ARGS_BYTES:
        return {}
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def load_manifest(text: str) -> PluginManifest:
    """Parse the manifest; explain an API without the v1.2 capabilities."""
    try:
        return parse_manifest(text)
    except ManifestError as error:
        if CAPABILITY_CARD not in api.KNOWN_CAPABILITIES:
            raise ManifestError(
                "this blueferry-plugin-api does not know the card and notify capabilities; "
                "the calendar plugin needs plugin API 1.2"
            ) from error
        raise
