"""Address -> photos. Tries each provider in order and returns the first with photos."""

from __future__ import annotations

from . import realtor, redfin, zillow
from .base import Found, ProviderError

PROVIDERS = {"redfin": redfin, "realtor": realtor, "zillow": zillow}
DEFAULT_ORDER = ["redfin", "realtor", "zillow"]


def find_photos(address: str, order: list[str] | None = None, timeout: float = 30.0, log=None):
    """Return (Found | None, attempts) where attempts is a list of (provider, outcome, message)."""
    attempts = []
    for name in order or DEFAULT_ORDER:
        mod = PROVIDERS[name]
        try:
            found = mod.lookup(address, timeout)
        except ProviderError as e:
            attempts.append((name, e.kind, str(e)))
            if log:
                log(f"  {name}: {e.kind} - {e}")
            continue
        except Exception as e:  # noqa: BLE001 - one broken provider shouldn't stop the others
            attempts.append((name, "error", f"{type(e).__name__}: {e}"))
            if log:
                log(f"  {name}: error - {type(e).__name__}: {e}")
            continue
        attempts.append((name, "ok", f"{len(found.photos)} photos for {found.match.address}"))
        if log:
            log(f"  {name}: found {found.match.address} ({len(found.photos)} photos) {found.match.url}")
        return found, attempts
    return None, attempts
