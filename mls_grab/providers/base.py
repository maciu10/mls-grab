"""Shared plumbing for address -> listing -> photos providers."""

from __future__ import annotations

import json
import re
import urllib.error
from dataclasses import dataclass, field

from ..core import fetch


class ProviderError(Exception):
    """A provider couldn't help. ``kind`` is one of: blocked, not_found, no_photos, error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class Match:
    provider: str
    address: str          # address as the provider spells it
    url: str              # human-facing listing page
    ids: dict = field(default_factory=dict)
    score: float = 0.0


@dataclass
class Found:
    match: Match
    photos: list[str]
    status: str = ""      # e.g. "For sale", "Sold" if known


# --------------------------------------------------------------------------- HTTP helpers

BOT_MARKERS = ("px-captcha", "_pxappid", "press &amp; hold", "press & hold", "captcha-delivery",
               "<title>just a moment...</title>", "access to this page has been denied",
               "request unsuccessful. incapsula")


def looks_blocked(body: bytes | str) -> bool:
    text = (body.decode("utf-8", "replace") if isinstance(body, bytes) else body)[:20000].lower()
    return any(m in text for m in BOT_MARKERS)


def get(url: str, *, timeout: float, headers: dict | None = None, data: bytes | None = None,
        provider: str = "") -> bytes:
    """GET (or POST when data is given). Turns bot walls and 403/429s into ProviderError('blocked')."""
    import urllib.request
    from ..core import USER_AGENT
    try:
        if data is None:
            body, _, _ = fetch(url, headers=headers, timeout=timeout, retries=2)
        else:
            h = {"User-Agent": USER_AGENT, "Content-Type": "application/json",
                 "Accept": "application/json", **(headers or {})}
            req = urllib.request.Request(url, data=data, headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 405, 429):
            raise ProviderError("blocked", f"{provider} refused the request (HTTP {e.code}); "
                                "it is probably blocking this server's IP") from e
        if e.code == 404:
            raise ProviderError("not_found", f"{provider}: HTTP 404 for {url}") from e
        raise ProviderError("error", f"{provider}: HTTP {e.code} for {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError("error", f"{provider}: network error: {getattr(e, 'reason', e)}") from e
    if looks_blocked(body) and not body.lstrip()[:1] in (b"{", b"["):
        raise ProviderError("blocked", f"{provider} answered with a bot check / captcha page")
    return body


def get_json(url: str, **kw):
    body = get(url, **kw).decode("utf-8", "replace").strip()
    # Redfin prefixes JSON with "{}&&" to stop JSON hijacking.
    if body.startswith("{}&&"):
        body = body[4:]
    try:
        return json.loads(body)
    except ValueError as e:
        raise ProviderError("error", f"{kw.get('provider', '')}: unexpected non-JSON response") from e


# --------------------------------------------------------------------------- address matching

_ABBR = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "lane": "ln", "court": "ct",
    "circle": "cir", "boulevard": "blvd", "place": "pl", "terrace": "ter", "parkway": "pkwy",
    "highway": "hwy", "square": "sq", "trail": "trl", "way": "way", "north": "n", "south": "s",
    "east": "e", "west": "w", "northeast": "ne", "northwest": "nw", "southeast": "se",
    "southwest": "sw", "apartment": "apt", "suite": "ste", "unit": "unit", "maine": "me",
}


def normalize_address(addr: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", addr.lower().replace("#", " unit "))
    return [_ABBR.get(w, w) for w in words]


def house_number(addr: str) -> str | None:
    m = re.match(r"\s*(\d+[a-z]?)\b", addr.lower())
    return m.group(1) if m else None


def zip_code(addr: str) -> str | None:
    m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", addr.strip())
    return m.group(1) if m else None


def address_score(query: str, candidate: str) -> float:
    """0..1 similarity. House number (and zip, when both have one) must agree or the score is 0."""
    qn, cn = house_number(query), house_number(candidate)
    if qn and cn and qn != cn:
        return 0.0
    qz, cz = zip_code(query), zip_code(candidate)
    if qz and cz and qz != cz:
        return 0.0
    q, c = set(normalize_address(query)), set(normalize_address(candidate))
    if not q or not c:
        return 0.0
    return len(q & c) / len(q)


def best_match(query: str, matches: list[Match], threshold: float = 0.6) -> Match | None:
    for m in matches:
        m.score = address_score(query, m.address)
    good = sorted((m for m in matches if m.score >= threshold), key=lambda m: -m.score)
    return good[0] if good else None
