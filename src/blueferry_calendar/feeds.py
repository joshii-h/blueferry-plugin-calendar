"""iCal subscription links: Google's "secret address in iCal format",
Outlook.com's published ICS, iCloud public calendars, holiday feeds.

The link itself is the secret (whoever knows it reads the calendar), so it
is never logged and never part of an error or reply: a :class:`FeedError`
names only the link's position and host.

The rules match the CalDAV side (:mod:`blueferry_plugin_kit.dav.caldav`):
https only (http on loopback, or anywhere with an explicit opt-in), the
link's own host plus the hosts the user allowed, at most five redirects,
each checked before it is followed, a 15 s timeout, 8 MiB per answer (also
after gzip), and no proxy unless the user opted in. ``webcal://`` and
``webcals://`` are read as https. Answers are revalidated with ETag and
Last-Modified, so an unchanged feed costs one 304.
"""
from __future__ import annotations

import hashlib
import http.client
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from blueferry_plugin_kit.dav.caldav import (
    MAX_REDIRECTS,
    MAX_XML_BYTES,
    TIMEOUT_SEC,
    CalDavError,
    Response,
    host_of,
)

MAX_LINKS = 8
MAX_LINK = 2048
MAX_FEED_BYTES = MAX_XML_BYTES
FETCH_EVERY = timedelta(minutes=15)
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_REDIRECTS = {301, 302, 303, 307, 308}

# GET url with headers and a timeout; the body is cut after MAX_FEED_BYTES + 1.
Get = Callable[[str, Mapping[str, str], float], Response]


class FeedError(CalDavError):
    """``token``: invalid-url, http-refused, too-many, not-found, forbidden,
    server-error, network, too-large, redirect, foreign-host, not-calendar.
    ``index`` is the link's position (0-based), ``host`` its host or, for
    foreign-host, the host that was not allowed. Never the link."""

    def __init__(self, token: str, index: int = 0, host: str = "") -> None:
        super().__init__(token, host)
        self.index = index

    @property
    def code(self) -> str:
        """``feed:<n>:<host>:<token>`` (1-based), what Status() remembers."""
        return f"feed:{self.index + 1}:{self.host}:{self.token}"


# ---- links ------------------------------------------------------------------------


def normalize_link(raw: str, *, allow_http: bool = False, index: int = 0) -> str:
    """The fetch URL of one link: webcal(s) -> https, no credentials, no fragment."""
    value = raw.strip()
    lowered = value.lower()
    for prefix in ("webcals://", "webcal://"):
        if lowered.startswith(prefix):
            value = "https://" + value[len(prefix):]
            break
    else:
        if "://" not in value:
            value = "https://" + value
    if len(value) > MAX_LINK or any(not ch.isprintable() or ch.isspace() for ch in value):
        raise FeedError("invalid-url", index)
    try:
        parts = urllib.parse.urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        _port = parts.port
    except ValueError:
        raise FeedError("invalid-url", index) from None
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http") or not host or parts.username or parts.password:
        raise FeedError("invalid-url", index)
    if scheme == "http" and host not in _LOOPBACK and not allow_http:
        raise FeedError("http-refused", index, host)
    return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


def parse_links(text: str, *, allow_http: bool = False) -> tuple[str, ...]:
    """Links separated by spaces or line breaks (a URL never holds a space)."""
    links = tuple(dict.fromkeys(
        normalize_link(raw, allow_http=allow_http, index=index)
        for index, raw in enumerate(text.split())
    ))
    if len(links) > MAX_LINKS:
        raise FeedError("too-many", MAX_LINKS)
    return links


def fingerprint(links: tuple[str, ...]) -> str:
    """Which links are configured (a hash; the links stay in the keyring)."""
    if not links:
        return ""
    return hashlib.sha256("\n".join(links).encode()).hexdigest()[:16]


def feed_id(link: str) -> str:
    """The calendar id of a link's events (a hash, never the link)."""
    return "ical-" + hashlib.sha256(link.encode()).hexdigest()[:16]


# ---- content ----------------------------------------------------------------------

_UNFOLD = re.compile(r"\r?\n[ \t]")
_CALNAME = re.compile(r"^X-WR-CALNAME(?:;[^:\r\n]*)?:(.*?)\r?$", re.MULTILINE | re.IGNORECASE)
_BEGIN_EVENT = re.compile(r"^BEGIN:VEVENT\r?$", re.MULTILINE | re.IGNORECASE)
_RECURRENCE_ID = re.compile(r"^RECURRENCE-ID[;:]", re.MULTILINE | re.IGNORECASE)


def looks_like_ical(text: str) -> bool:
    head = text.lstrip("﻿ \t\r\n")[:15].upper()
    return head == "BEGIN:VCALENDAR" and "END:VCALENDAR" in text[-4096:].upper()


def calendar_name(text: str) -> str:
    """``X-WR-CALNAME`` (Google, Outlook, iCloud), plain text, or ""."""
    match = _CALNAME.search(_UNFOLD.sub("", text[:65536]))
    if not match:
        return ""
    value = re.sub(r"\\([,;\\])", r"\1", match.group(1)).replace("\\n", " ").replace("\\N", " ")
    value = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(value.split())[:200]


def event_count(text: str) -> int:
    """Events in the feed; a moved instance (RECURRENCE-ID) is no new event."""
    return max(0, len(_BEGIN_EVENT.findall(text)) - len(_RECURRENCE_ID.findall(text)))


# ---- transport --------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None  # FeedClient checks and follows redirects itself


def urllib_get(
    url: str, headers: Mapping[str, str], timeout: float, *, proxy: bool = False,
    context: ssl.SSLContext | None = None,
) -> Response:
    handlers: list[Any] = [_NoRedirect, urllib.request.HTTPSHandler(context=context)]
    if not proxy:
        handlers.append(urllib.request.ProxyHandler({}))   # not http(s)_proxy
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, method="GET", headers=dict(headers))  # nosec B310
    try:
        reply = opener.open(request, timeout=timeout)  # nosec B310 - scheme checked
    except urllib.error.HTTPError as error:
        reply = error
    try:
        data = reply.read(MAX_FEED_BYTES + 1)
        status = reply.status if hasattr(reply, "status") else reply.code
        merged: dict[str, str] = {}
        for name, value in reply.headers.items():
            key = name.lower()
            merged[key] = merged[key] + "\n" + value if key in merged else value
        return Response(int(status), merged, bytes(data), url)
    finally:
        reply.close()


def _decoded(reply: Response, index: int, host: str) -> str:
    data = reply.body
    if len(data) > MAX_FEED_BYTES:
        raise FeedError("too-large", index, host)
    encoding = reply.headers.get("content-encoding", "").strip().lower()
    if encoding in ("gzip", "x-gzip"):
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            data = inflater.decompress(data, MAX_FEED_BYTES + 1)
        except zlib.error:
            raise FeedError("not-calendar", index, host) from None
        if len(data) > MAX_FEED_BYTES or inflater.unconsumed_tail:
            raise FeedError("too-large", index, host)
    elif encoding not in ("", "identity"):
        raise FeedError("not-calendar", index, host)
    return data.decode("utf-8", errors="replace").lstrip("﻿")


@dataclass(slots=True)
class Feed:
    """One fetched link (kept in memory only; the link is the dict key)."""

    text: str
    etag: str = ""
    modified: str = ""
    hosts: frozenset[str] = frozenset()   # other hosts its redirects went to
    name: str = ""                        # X-WR-CALNAME
    count: int = 0                        # events in the feed
    expanded: dict[tuple[str, str], list[Any]] = field(default_factory=dict)


class FeedClient:
    def __init__(
        self, *, hosts: tuple[str, ...] = (), allow_http: bool = False, use_proxy: bool = False,
        get: Get | None = None, timeout: float = TIMEOUT_SEC, user_agent: str = "",
        context: ssl.SSLContext | None = None,
    ) -> None:
        self.hosts = frozenset(h.lower().rstrip(".") for h in hosts)
        self.allow_http = allow_http
        self._get = get or (
            lambda url, headers, seconds: urllib_get(
                url, headers, seconds, proxy=use_proxy, context=context,
            )
        )
        self._timeout = timeout
        self.user_agent = user_agent

    def _allowed(self, url: str, own: str) -> bool:
        try:
            parts = urllib.parse.urlsplit(url)
            host = (parts.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if parts.username or parts.password or not host:
            return False
        if host != own and host not in self.hosts:
            return False
        if parts.scheme == "http":
            return self.allow_http or host in _LOOPBACK
        return parts.scheme == "https"

    def fetch(self, link: str, index: int = 0, previous: Feed | None = None) -> Feed:
        """GET one link, revalidating ``previous``; raises FeedError."""
        own = host_of(link)
        url, seen = link, set()
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                host = host_of(url)
            except ValueError:
                raise FeedError("redirect", index, own) from None
            if not self._allowed(url, own):
                if host and (host == own or host in self.hosts):
                    raise FeedError("http-refused", index, host)
                raise FeedError("foreign-host", index, host)
            headers = {
                "Accept": "text/calendar, text/plain;q=0.5, */*;q=0.1",
                "Accept-Encoding": "gzip",
            }
            if self.user_agent:
                headers["User-Agent"] = self.user_agent
            if previous is not None and previous.etag:
                headers["If-None-Match"] = previous.etag
            if previous is not None and previous.modified:
                headers["If-Modified-Since"] = previous.modified
            try:
                reply = self._get(url, headers, self._timeout)
            except FeedError:
                raise
            except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
                raise FeedError("network", index, own) from None
            if host != own:
                seen.add(host)
            if reply.status in _REDIRECTS:
                location = reply.headers.get("location", "").split("\n")[0].strip()
                if not location:
                    raise FeedError("redirect", index, own)
                try:
                    url = urllib.parse.urljoin(url, location)
                except ValueError:
                    raise FeedError("redirect", index, own) from None
                continue
            if reply.status == 304 and previous is not None:
                previous.hosts = frozenset(seen)
                return previous
            if reply.status in (401, 403):
                raise FeedError("forbidden", index, own)
            if reply.status in (404, 410):
                raise FeedError("not-found", index, own)
            if not 200 <= reply.status < 300:
                raise FeedError("server-error", index, own)
            text = _decoded(reply, index, own)
            if not looks_like_ical(text):
                raise FeedError("not-calendar", index, own)
            return Feed(
                text=text,
                etag=reply.headers.get("etag", "").split("\n")[0].strip()[:200],
                modified=reply.headers.get("last-modified", "").split("\n")[0].strip()[:100],
                hosts=frozenset(seen), name=calendar_name(text), count=event_count(text),
            )
        raise FeedError("redirect", index, own)
