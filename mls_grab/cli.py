"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import zipfile

from . import __version__, providers, sites
from .core import (Options, default_out_dir, download_all, eprint, extract_candidates, fetch,
                   select_photos)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mls-grab",
        description="Download all photos of a home listing, given its street address or listing URL.",
        epilog=(
            "examples:\n"
            "  mls-grab '1 Tribou Drive, Paris, ME 04281'\n"
            "  mls-grab '1 Tribou Dr, Paris ME' --zip --json\n"
            "  mls-grab https://www.redfin.com/ME/Paris/1-Tribou-Dr-04281/home/123456\n"
            "  mls-grab 1683368 --url-template 'https://example-mls.com/listing/{id}'\n"
            "  mls-grab --html saved-page.html --base-url https://www.zillow.com/homedetails/..._zpid\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("target", nargs="?",
                   help="street address (with city/state or ZIP), listing page URL, or a listing ID "
                        "with --url-template / --site")
    p.add_argument("-a", "--address", action="store_true",
                   help="treat TARGET as an address even if it looks like something else")
    p.add_argument("-p", "--providers", default=",".join(providers.DEFAULT_ORDER),
                   help="address lookup order (default: %(default)s)")
    p.add_argument("-t", "--url-template", default=os.environ.get("MLS_GRAB_URL_TEMPLATE"),
                   help="URL pattern with {id}, e.g. 'https://site/listing/{id}'")
    p.add_argument("-s", "--site", choices=sorted(sites.ADAPTERS),
                   help="site preset: build the URL from a bare ID (e.g. a Zillow zpid)")
    p.add_argument("--generic", action="store_true",
                   help="skip site-specific page adapters and use only the generic extractor")
    p.add_argument("--html", metavar="FILE",
                   help="parse a saved HTML file instead of fetching the page")
    p.add_argument("--base-url", help="page URL used to resolve relative links with --html")
    p.add_argument("-o", "--out", help="output directory (default: derived from address / listing)")
    p.add_argument("-z", "--zip", action="store_true", help="also write photos.zip into the output dir")
    p.add_argument("-l", "--list", action="store_true", help="print photo URLs, don't download")
    p.add_argument("--json", action="store_true",
                   help="print a JSON summary to stdout (progress still goes to stderr)")
    p.add_argument("--min-bytes", type=int, default=20_000,
                   help="skip downloaded files smaller than this (default 20000)")
    p.add_argument("--min-dim", type=int, default=300,
                   help="skip images whose longest side is below this many px (default 300)")
    p.add_argument("--no-og", action="store_true", help="ignore og:image / twitter:image meta tags")
    p.add_argument("--strip-resize", action="store_true",
                   help="drop CDN resize query params (?w=, ?h=, ...) to request originals")
    p.add_argument("-H", "--header", action="append", default=[], metavar="'Name: value'",
                   help="extra HTTP header for page/photo requests (repeatable)")
    p.add_argument("-j", "--workers", type=int, default=6, help="parallel downloads (default 6)")
    p.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    p.add_argument("--retries", type=int, default=3, help="attempts per request (default 3)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


def looks_like_address(s: str) -> bool:
    s = s.strip()
    return bool(re.match(r"^\d+[a-zA-Z]?\s+\S", s)) and bool(re.search(r"[A-Za-z]{2,}", s))


def slugify(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:80] or "listing"


class Fail(Exception):
    def __init__(self, msg: str, code: int = 1, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary: dict = {"ok": False}
    try:
        code = _run(args, summary)
    except Fail as e:
        eprint(f"error: {e}")
        summary.update(error=str(e), **e.extra)
        code = e.code
    if args.json:
        print(json.dumps(summary, indent=2))
    return code


def _run(args, summary: dict) -> int:
    headers = {}
    for h in args.header:
        if ":" not in h:
            raise Fail(f"bad --header {h!r}, expected 'Name: value'", 2)
        k, v = h.split(":", 1)
        headers[k.strip()] = v.strip()

    target = (args.target or "").strip()
    listing_id = None
    page_url = args.base_url or ""
    found = None

    # ---- resolve what to download ------------------------------------------------------
    if target and (args.address or (not target.startswith(("http://", "https://")) and not args.site
                                    and "_zpid" not in target.lower() and not args.url_template
                                    and not args.html)):
        if not args.address and not looks_like_address(target):
            raise Fail(f"{target!r} doesn't look like a street address or URL. Give a full address "
                       "like '1 Tribou Drive, Paris, ME 04281', a listing URL, or use "
                       "--url-template / --site for a bare listing ID.", 2)
        order = [p.strip() for p in args.providers.split(",") if p.strip()]
        bad = [p for p in order if p not in providers.PROVIDERS]
        if bad:
            raise Fail(f"unknown provider(s) {', '.join(bad)}; choose from "
                       f"{', '.join(providers.PROVIDERS)}", 2)
        eprint(f"Looking up {target!r} on {', '.join(order)} ...")
        found, attempts = providers.find_photos(target, order, timeout=args.timeout, log=eprint)
        summary["attempts"] = [{"provider": p, "outcome": o, "message": m} for p, o, m in attempts]
        summary["address_query"] = target
        if not found:
            blocked = [p for p, o, _ in attempts if o == "blocked"]
            hint = (f" ({', '.join(blocked)} blocked this server; if you have the listing URL, "
                    "save the page in a browser and use --html)") if blocked else ""
            raise Fail(f"no listing photos found for {target!r}{hint}")
        photos = found.photos
        page_url = found.match.url
        summary.update(provider=found.match.provider, listing_url=found.match.url,
                       address=found.match.address, status=found.status)
        default_out = slugify(found.match.address or target)
    else:
        if target.startswith(("http://", "https://")):
            page_url = target
        elif target and (args.site or "_zpid" in target.lower()):
            adapter = sites.ADAPTERS[args.site or "zillow"]
            listing_id = target.lower().replace("_zpid", "")
            page_url = adapter.listing_url(target)
        elif target and args.url_template:
            listing_id = target
            page_url = args.url_template.replace("{id}", urllib.parse.quote(listing_id))
        elif target:
            listing_id = target
        if not args.html and not page_url:
            build_parser().print_usage(sys.stderr)
            raise Fail("nothing to do", 2)
        photos = _photos_from_page(args, page_url, headers)
        if args.html and not page_url:
            page_url = ""
        summary["listing_url"] = page_url or None
        default_out = default_out_dir(page_url or (args.html or ""), listing_id)

    summary["photo_urls"] = photos
    if args.list:
        summary["ok"] = True
        if not args.json:
            print("\n".join(photos))
        return 0

    # ---- download -------------------------------------------------------------------------
    out = args.out or default_out
    eprint(f"Downloading {len(photos)} photo(s) to {out}/")
    opts = Options(out_dir=out, min_bytes=args.min_bytes, min_dim=args.min_dim,
                   workers=args.workers, timeout=args.timeout, retries=max(1, args.retries),
                   strip_resize=args.strip_resize,
                   referer=page_url if page_url.startswith("http") else None,
                   headers=headers, verbose=args.verbose)
    results = download_all(photos, opts, log=eprint)
    saved = [r for r in results if r.path]
    failed = [r for r in results if r.error]
    skipped = [r for r in results if r.skipped]
    eprint(f"Saved {len(saved)}, skipped {len(skipped)}, failed {len(failed)} -> {out}/")
    summary.update(out_dir=os.path.abspath(out), saved=len(saved), skipped=len(skipped),
                   failed=len(failed),
                   files=[{"file": os.path.basename(r.path), "url": r.url, "bytes": r.size} for r in saved])
    if not saved:
        raise Fail("photos were found but none could be downloaded", 1, **{k: summary[k] for k in ("out_dir",)})

    meta = {k: summary.get(k) for k in ("address", "provider", "listing_url", "status")}
    meta["photos"] = summary["files"]
    with open(os.path.join(out, "sources.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    if args.zip:
        zpath = os.path.join(out, "photos.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:  # JPEGs don't compress
            for r in saved:
                z.write(r.path, os.path.basename(r.path))
            z.write(os.path.join(out, "sources.json"), "sources.json")
        summary["zip"] = os.path.abspath(zpath)
        eprint(f"Wrote {zpath}")
    summary["ok"] = True
    return 0


def _photos_from_page(args, page_url: str, headers: dict) -> list[str]:
    try:
        if args.html:
            with open(args.html, "rb") as fh:
                raw = fh.read()
            parse_url = page_url or "file://" + os.path.abspath(args.html)
        else:
            if args.verbose:
                eprint(f"Fetching {page_url}")
            raw, _, parse_url = fetch(page_url, headers=headers, timeout=args.timeout,
                                      retries=max(1, args.retries),
                                      accept="text/html,application/xhtml+xml,*/*;q=0.8")
    except OSError as e:
        msg = f"could not load listing page: {e}"
        if getattr(e, "code", None) in (401, 403, 429):
            msg += ("\nThe site is blocking automated requests. Try the street address instead "
                    "(other sites may allow it), or save the page in your browser (Save Page As -> "
                    f"HTML only) and run: mls-grab --html saved.html --base-url '{page_url}'")
        raise Fail(msg) from e
    page = raw.decode("utf-8", errors="replace")

    adapter = None if args.generic else sites.for_url(parse_url)
    photos: list[str] = []
    if adapter:
        reason = adapter.blocked(page)
        if reason:
            raise Fail(reason)
        photos = adapter.extract(page, parse_url)
        if args.verbose or not photos:
            eprint(f"{adapter.__name__.rsplit('.', 1)[-1]} adapter: {len(photos)} photos"
                   + ("" if photos else "; falling back to generic extraction"))
    if not photos:
        cands = extract_candidates(page, parse_url, include_og=not args.no_og)
        photos = select_photos(cands, strip_resize=args.strip_resize)
        if args.verbose:
            eprint(f"{len(cands)} image references, {len(photos)} unique photos")
    if not photos:
        raise Fail("No photos found. The gallery may be loaded by JavaScript after the page loads: "
                   "save the fully loaded page from your browser and rerun with --html FILE.")
    return photos
