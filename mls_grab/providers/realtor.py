"""Realtor.com.

1. ``parser-external.geo.moveaws.com/suggest?input=<address>&area_types=address`` -> property_id
2. GraphQL ``home(property_id) { photos(https: true) { href } }`` on realtor.com

Photo hrefs look like ``https://ap.rdcpix.com/<hash>l-m123456789s.jpg``; the trailing ``s``
is the small size, and ``rd-w2048_h1536`` asks the CDN for a large version.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from .base import Found, Match, ProviderError, best_match, get, get_json

NAME = "realtor"
SUGGEST = "https://parser-external.geo.moveaws.com/suggest"
GRAPHQL = "https://www.realtor.com/api/v1/rdc_search_srp?client_id=rdc-search-new-communities&schema=vesta"

QUERY = """query Home($property_id: ID!) {
  home(property_id: $property_id) {
    property_id
    href
    status
    location { address { line city state_code postal_code } }
    photos(https: true) { href }
  }
}"""


def search(address: str, timeout: float) -> list[Match]:
    q = urllib.parse.urlencode({"input": address, "client_id": "rdc-home", "limit": 10,
                                "area_types": "address"})
    data = get_json(f"{SUGGEST}?{q}", timeout=timeout, provider=NAME)
    out = []
    for r in data.get("autocomplete") or []:
        pid = r.get("mpr_id") or r.get("property_id")
        if not pid:
            continue
        addr = r.get("full_address") or [" ".join(str(r.get(k, "")) for k in
                                                   ("line", "city", "state_code", "postal_code"))]
        addr = addr[0] if isinstance(addr, list) else addr
        out.append(Match(NAME, addr, f"https://www.realtor.com/realestateandhomes-detail/M{pid}",
                         {"property_id": str(pid)}))
    return out


def large(href: str) -> str:
    return re.sub(r"s\.(jpg|jpeg|png|webp)$", r"rd-w2048_h1536.\1", href)


def photos(match: Match, timeout: float) -> Found:
    body = json.dumps({"query": QUERY, "variables": {"property_id": match.ids["property_id"]}})
    data = json.loads(get(GRAPHQL, data=body.encode(), timeout=timeout, provider=NAME,
                          headers={"Origin": "https://www.realtor.com",
                                   "Referer": "https://www.realtor.com/"}))
    home = (data.get("data") or {}).get("home") or {}
    if not home:
        errs = "; ".join(e.get("message", "") for e in data.get("errors") or [])
        raise ProviderError("error", f"{NAME}: no property data{': ' + errs if errs else ''}")
    if home.get("href"):
        match.url = home["href"]
    urls = [large(p["href"]) for p in home.get("photos") or [] if p.get("href")]
    if not urls:
        raise ProviderError("no_photos", f"{NAME}: listing found but it has no photos")
    return Found(match, urls, str(home.get("status") or "").replace("_", " "))


def lookup(address: str, timeout: float) -> Found:
    m = best_match(address, search(address, timeout))
    if not m:
        raise ProviderError("not_found", f"{NAME}: no home matching {address!r}")
    return photos(m, timeout)
