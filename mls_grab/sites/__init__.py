"""Site-specific adapters. Each module exposes:

    matches(url) -> bool
    listing_url(listing_id) -> str        # build a page URL from a bare ID
    blocked(html) -> str | None           # explain bot-wall / captcha pages
    extract(html, url) -> list[str]       # photo URLs for *this* listing only, largest size

When an adapter returns photos they are used as-is; otherwise the generic extractor runs.
"""

from . import zillow

ADAPTERS = {"zillow": zillow}


def for_url(url: str):
    for mod in ADAPTERS.values():
        if mod.matches(url):
            return mod
    return None
