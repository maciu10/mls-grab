"""Redfin.

1. ``/stingray/do/location-autocomplete?location=<address>&v=2`` -> home URL ("/ME/Paris/.../home/123")
2. ``/stingray/api/home/details/initialInfo?path=<url>``          -> propertyId, listingId
3. ``/stingray/api/home/details/aboveTheFold?propertyId=&listingId=`` -> mediaBrowserInfo.photos
   (photoUrls.fullScreenPhotoUrl is the largest size)

If the JSON API changes, it falls back to scanning the home page HTML.
"""

from __future__ import annotations

import re
import urllib.parse

from ..core import extract_candidates, select_photos
from .base import Found, Match, ProviderError, best_match, get, get_json

NAME = "redfin"
BASE = "https://www.redfin.com"


def search(address: str, timeout: float) -> list[Match]:
    q = urllib.parse.urlencode({"location": address, "start": 0, "count": 10, "v": 2})
    data = get_json(f"{BASE}/stingray/do/location-autocomplete?{q}", timeout=timeout, provider=NAME)
    payload = data.get("payload") or {}
    rows = []
    if payload.get("exactMatch"):
        rows.append(payload["exactMatch"])
    for sec in payload.get("sections") or []:
        rows.extend(sec.get("rows") or [])
    out = []
    for r in rows:
        url = r.get("url") or ""
        if "/home/" not in url:
            continue  # cities, schools, neighborhoods...
        addr = " ".join(x for x in (r.get("name"), r.get("subName")) if x)
        out.append(Match(NAME, addr, urllib.parse.urljoin(BASE, url), {"path": url, "id": r.get("id")}))
    return out


def photos(match: Match, timeout: float) -> Found:
    path = match.ids["path"]
    status = ""
    urls: list[str] = []
    try:
        info = get_json(f"{BASE}/stingray/api/home/details/initialInfo?"
                        + urllib.parse.urlencode({"path": path}), timeout=timeout, provider=NAME)
        p = info.get("payload") or {}
        prop_id, listing_id = p.get("propertyId"), p.get("listingId")
        if not prop_id:
            m = re.search(r"/home/(\d+)", path)
            prop_id = m.group(1) if m else None
        q = {"propertyId": prop_id, "accessLevel": 1}
        if listing_id:
            q["listingId"] = listing_id
        atf = get_json(f"{BASE}/stingray/api/home/details/aboveTheFold?" + urllib.parse.urlencode(q),
                       timeout=timeout, provider=NAME)
        ap = atf.get("payload") or {}
        for ph in (ap.get("mediaBrowserInfo") or {}).get("photos") or []:
            pu = ph.get("photoUrls") or {}
            u = pu.get("fullScreenPhotoUrl") or pu.get("nonFullScreenPhotoUrl") or pu.get("lightboxListUrl")
            if u:
                urls.append(u)
        status = ((ap.get("addressSectionInfo") or {}).get("status") or {}).get("displayValue", "")
    except ProviderError as e:
        if e.kind == "blocked":
            raise
    if not urls:  # fall back to the home page itself
        body = get(match.url, timeout=timeout, provider=NAME).decode("utf-8", "replace")
        cands = [c for c in extract_candidates(body, match.url, include_og=False)
                 if "cdn-redfin.com/photo/" in c.url]
        urls = select_photos(cands)
    if not urls:
        raise ProviderError("no_photos", f"{NAME}: listing found but it has no photos")
    return Found(match, _dedupe(urls), status)


def _dedupe(urls):
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def lookup(address: str, timeout: float) -> Found:
    m = best_match(address, search(address, timeout))
    if not m:
        raise ProviderError("not_found", f"{NAME}: no home matching {address!r}")
    return photos(m, timeout)
