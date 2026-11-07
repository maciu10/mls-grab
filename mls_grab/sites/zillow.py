"""Zillow (zillow.com/homedetails/..._zpid).

Zillow renders listing data into a Next.js ``__NEXT_DATA__`` script (older pages:
``hdpApolloPreloadedData``). The property record sits inside ``gdpClientCache``, which is a
JSON *string* nested inside that JSON. Its photos are in ``responsivePhotos`` / ``originalPhotos``
/ ``photos``, and each photo carries ``mixedSources.{jpeg,webp}``, a list of ``{url, width}``.

The page also holds photos of nearby and similar homes, so we only take the photos of the
record whose ``zpid`` matches the URL.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

HOST_RE = re.compile(r"^https?://(?:www\.)?zillow\.com/", re.I)
ZPID_RE = re.compile(r"(\d+)_zpid", re.I)
PHOTO_LIST_KEYS = ("originalPhotos", "responsivePhotos", "photos", "hugePhotos", "bigPhotos")


def matches(url: str) -> bool:
    return bool(HOST_RE.match(url or ""))


def listing_url(listing_id: str) -> str:
    zpid = ZPID_RE.search(listing_id)
    return f"https://www.zillow.com/homedetails/{zpid.group(1) if zpid else listing_id}_zpid/"


def zpid_from_url(url: str) -> str | None:
    m = ZPID_RE.search(url or "")
    return m.group(1) if m else None


def blocked(page: str) -> str | None:
    head = page[:20000].lower()
    if ("px-captcha" in head or "press &amp; hold" in head or "press & hold" in head
            or "perimeterx" in head or "_pxappid" in head or "access to this page has been denied" in head):
        return ("Zillow served its bot check (PerimeterX captcha) instead of the listing. Open the "
                "listing in your browser, save it (Save Page As → HTML only), and rerun with "
                "--html FILE --base-url URL.")
    return None


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._on = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            a = dict(attrs)
            sid, typ = (a.get("id") or ""), (a.get("type") or "").lower()
            self._on = sid in ("__NEXT_DATA__", "hdpApolloPreloadedData") or typ == "application/json"
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._on:
            self.blocks.append("".join(self._buf))
            self._on = False

    def handle_data(self, data):
        if self._on:
            self._buf.append(data)


def _decode_nested(obj, depth=0):
    """Recursively parse string values that are themselves JSON (gdpClientCache, apiCache)."""
    if depth > 6:
        return obj
    if isinstance(obj, dict):
        return {k: _decode_nested(v, depth) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_nested(v, depth) for v in obj]
    if isinstance(obj, str) and len(obj) > 50 and obj.lstrip()[:1] in "{[":
        try:
            return _decode_nested(json.loads(obj), depth + 1)
        except ValueError:
            return obj
    return obj


def _iter_dicts(obj):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _best_url(photo) -> str | None:
    """Largest JPEG from a photo record (falls back to webp, then a bare url)."""
    if isinstance(photo, str):
        return photo if photo.startswith("http") else None
    if not isinstance(photo, dict):
        return None
    ms = photo.get("mixedSources") or {}
    for fmt in ("jpeg", "webp"):
        srcs = [s for s in ms.get(fmt) or [] if isinstance(s, dict) and s.get("url")]
        if srcs:
            return max(srcs, key=lambda s: s.get("width") or 0)["url"]
    for k in ("url", "fullUrl", "hiResUrl", "largeUrl", "src"):
        v = photo.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def _photos_of(record: dict) -> list[str]:
    for key in PHOTO_LIST_KEYS:
        lst = record.get(key)
        if isinstance(lst, list) and lst:
            urls = [u for u in (_best_url(p) for p in lst) if u]
            if urls:
                return urls
    return []


def extract(page: str, url: str) -> list[str]:
    p = _Scripts()
    p.feed(page)
    p.close()
    target = zpid_from_url(url)
    best: list[str] = []
    for block in p.blocks:
        try:
            data = _decode_nested(json.loads(block))
        except ValueError:
            continue
        for d in _iter_dicts(data):
            photos = _photos_of(d)
            if not photos:
                continue
            if target and str(d.get("zpid", "")) == target:
                return _dedupe(photos)
            # No zpid match (e.g. --html without --base-url): remember the biggest gallery.
            if not target and len(photos) > len(best):
                best = photos
    return _dedupe(best)


def _dedupe(urls: list[str]) -> list[str]:
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
