"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse

from . import __version__
from .core import (Options, default_out_dir, download_all, eprint, extract_candidates, fetch,
                   select_photos)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mls-grab",
        description="Download all photos from an MLS / real-estate listing page.",
        epilog=(
            "examples:\n"
            "  mls-grab https://example-mls.com/listing/1683368\n"
            "  mls-grab 1683368 --url-template 'https://example-mls.com/listing/{id}'\n"
            "  mls-grab --html saved-page.html --base-url https://example-mls.com/listing/1683368\n"
            "  mls-grab URL --list            # just print the photo URLs\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("target", nargs="?",
                   help="listing page URL, or a listing ID when --url-template is given "
                        "(or $MLS_GRAB_URL_TEMPLATE is set)")
    p.add_argument("-t", "--url-template",
                   default=os.environ.get("MLS_GRAB_URL_TEMPLATE"),
                   help="URL pattern with {id}, e.g. 'https://site/listing/{id}'")
    p.add_argument("--html", metavar="FILE",
                   help="parse a saved HTML file instead of fetching the page (use when the site "
                        "blocks scripts or needs a login; save the page from your browser)")
    p.add_argument("--base-url", help="page URL used to resolve relative links with --html")
    p.add_argument("-o", "--out", help="output directory (default: listing-<id>)")
    p.add_argument("-l", "--list", action="store_true", help="print photo URLs, don't download")
    p.add_argument("--json", action="store_true", help="with --list, output JSON")
    p.add_argument("--min-bytes", type=int, default=20_000,
                   help="skip downloaded files smaller than this (default 20000)")
    p.add_argument("--min-dim", type=int, default=300,
                   help="skip images whose longest side is below this many px (default 300)")
    p.add_argument("--no-og", action="store_true", help="ignore og:image / twitter:image meta tags")
    p.add_argument("--strip-resize", action="store_true",
                   help="drop CDN resize query params (?w=, ?h=, ...) to request originals")
    p.add_argument("-H", "--header", action="append", default=[], metavar="'Name: value'",
                   help="extra HTTP header for all requests (repeatable), e.g. a Cookie")
    p.add_argument("-j", "--workers", type=int, default=6, help="parallel downloads (default 6)")
    p.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    p.add_argument("--retries", type=int, default=3, help="attempts per request (default 3)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    headers = {}
    for h in args.header:
        if ":" not in h:
            eprint(f"bad --header {h!r}, expected 'Name: value'")
            return 2
        k, v = h.split(":", 1)
        headers[k.strip()] = v.strip()

    listing_id = None
    page_url = args.base_url or ""
    if args.target:
        if args.target.startswith(("http://", "https://")):
            page_url = args.target
        elif args.url_template:
            listing_id = args.target
            page_url = args.url_template.replace("{id}", urllib.parse.quote(listing_id))
        elif not args.html:
            eprint(f"{args.target!r} is not a URL. Pass the listing page URL, or give "
                   "--url-template 'https://site/path/{id}' to use a bare listing ID.")
            return 2
        else:
            listing_id = args.target
    if not args.html and not page_url:
        build_parser().print_usage(sys.stderr)
        return 2

    # 1. Get the page.
    try:
        if args.html:
            with open(args.html, "rb") as fh:
                raw = fh.read()
            page_url = page_url or "file://" + os.path.abspath(args.html)
        else:
            if args.verbose:
                eprint(f"Fetching {page_url}")
            raw, _, page_url = fetch(page_url, headers=headers, timeout=args.timeout, retries=max(1, args.retries),
                                     accept="text/html,application/xhtml+xml,*/*;q=0.8")
    except Exception as e:  # noqa: BLE001
        eprint(f"error: could not load listing page: {e}")
        return 1
    page = raw.decode("utf-8", errors="replace")

    # 2. Find photos.
    cands = extract_candidates(page, page_url, include_og=not args.no_og)
    photos = select_photos(cands, strip_resize=args.strip_resize)
    if args.verbose:
        eprint(f"{len(cands)} image references, {len(photos)} unique photos")

    if not photos:
        eprint("No photos found. The gallery may be loaded by JavaScript after the page loads: "
               "save the fully loaded page from your browser and rerun with --html FILE.")
        return 1

    if args.list:
        if args.json:
            print(json.dumps(photos, indent=2))
        else:
            print("\n".join(photos))
        return 0

    # 3. Download.
    out = args.out or default_out_dir(page_url, listing_id)
    eprint(f"Downloading {len(photos)} photo(s) to {out}/")
    opts = Options(out_dir=out, min_bytes=args.min_bytes, min_dim=args.min_dim,
                   workers=args.workers, timeout=args.timeout, retries=max(1, args.retries), strip_resize=args.strip_resize,
                   referer=page_url if page_url.startswith("http") else None,
                   headers=headers, verbose=args.verbose)
    results = download_all(photos, opts, log=eprint)
    saved = [r for r in results if r.path]
    failed = [r for r in results if r.error]
    skipped = [r for r in results if r.skipped]
    eprint(f"Saved {len(saved)}, skipped {len(skipped)}, failed {len(failed)} -> {out}/")
    if saved:
        with open(os.path.join(out, "sources.json"), "w") as fh:
            json.dump({"page": page_url,
                       "photos": [{"file": os.path.basename(r.path), "url": r.url} for r in saved]},
                      fh, indent=2)
    return 0 if saved else 1
