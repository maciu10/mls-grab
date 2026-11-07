"""Zillow: public autocomplete gives the zpid; the listing page then usually hits a captcha
from servers, in which case we report 'blocked' and move on."""

from __future__ import annotations

import urllib.parse

from ..sites import zillow as page
from .base import Found, Match, ProviderError, best_match, get, get_json

NAME = "zillow"
AUTOCOMPLETE = "https://www.zillowstatic.com/autocomplete/v3/suggestions"


def search(address: str, timeout: float) -> list[Match]:
    q = urllib.parse.urlencode({"q": address, "resultTypes": "allAddress", "resultCount": 10})
    data = get_json(f"{AUTOCOMPLETE}?{q}", timeout=timeout, provider=NAME)
    out = []
    for r in data.get("results") or []:
        zpid = (r.get("metaData") or {}).get("zpid")
        if zpid:
            out.append(Match(NAME, r.get("display", ""), page.listing_url(str(zpid)), {"zpid": str(zpid)}))
    return out


def photos(match: Match, timeout: float) -> Found:
    body = get(match.url, timeout=timeout, provider=NAME).decode("utf-8", "replace")
    if page.blocked(body):
        raise ProviderError("blocked", f"{NAME} answered with a bot check / captcha page")
    urls = page.extract(body, match.url)
    if not urls:
        raise ProviderError("no_photos", f"{NAME}: listing found but no photos in the page")
    return Found(match, urls)


def lookup(address: str, timeout: float) -> Found:
    m = best_match(address, search(address, timeout))
    if not m:
        raise ProviderError("not_found", f"{NAME}: no home matching {address!r}")
    return photos(m, timeout)
