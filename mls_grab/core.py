"""Core logic: fetch a listing page, extract photo URLs, download them."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic", ".bmp", ".tif", ".tiff")

# Filename/path fragments that almost always mean "not a listing photo".
JUNK_PATTERNS = re.compile(
    r"(?:^|[/_\-.])(?:logo|icon|favicon|sprite|avatar|badge|placeholder|spinner|"
    r"loading|pixel|blank|spacer|tracking|beacon|emoji|flag|arrow|button|social|"
    r"facebook|twitter|instagram|linkedin|pinterest|youtube|equal-housing|eho|"
    r"realtor-logo|mls-logo|headshot|agent-photo|brokerage)(?:[/_\-.\d]|$)",
    re.IGNORECASE,
)

# Absolute image URLs embedded anywhere (inline JSON, JS, CSS). Handles JSON-escaped slashes.
URL_IN_TEXT = re.compile(
    r"""https?:(?:\\?/){2}[^\s"'<>()\\]+?(?:\\?/[^\s"'<>()\\]*?)*?"""
    r"""\.(?:jpe?g|png|webp|gif|avif)(?:\?[^\s"'<>()\\]*)?(?=["'\s<>()\\,]|$)""",
    re.IGNORECASE,
)
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)

# JSON keys that commonly hold listing photos in JSON-LD / embedded state.
PHOTO_KEYS = {
    "image", "images", "photo", "photos", "photourl", "photourls", "mediaurl",
    "media", "largeurl", "largephotourl", "highresurl", "hiresurl", "fullurl",
    "originalurl", "url", "src", "contenturl", "thumbnailurl", "uri", "href",
}


@dataclass
class Candidate:
    url: str
    source: str           # where it was found (jsonld, og, img, srcset, script, ...)
    width_hint: int = 0   # from srcset descriptor / width attr, if known
    order: int = 0        # discovery order, used to keep gallery order stable


@dataclass
class Result:
    url: str
    path: str | None = None
    error: str | None = None
    skipped: str | None = None
    size: int = 0


@dataclass
class Options:
    out_dir: str
    min_bytes: int = 20_000
    min_dim: int = 300
    workers: int = 6
    retries: int = 3
    timeout: float = 30.0
    include_og: bool = True
    strip_resize: bool = False
    referer: str | None = None
    headers: dict = field(default_factory=dict)
    verbose: bool = False


# --------------------------------------------------------------------------- HTTP

def _request(url: str, referer: str | None = None, headers: dict | None = None) -> urllib.request.Request:
    h = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    return urllib.request.Request(url, headers=h)


def fetch(url: str, *, referer=None, headers=None, timeout=30.0, retries=3, accept=None) -> tuple[bytes, str, str]:
    """Return (body, content_type, final_url). Retries on transient errors."""
    hdrs = dict(headers or {})
    if accept:
        hdrs.setdefault("Accept", accept)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(_request(url, referer, hdrs), timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", ""), resp.geturl()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (408, 425, 429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(min(2 ** attempt, 8))
    assert last is not None
    raise last


# --------------------------------------------------------------------------- extraction

class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base: str | None = None
        self.candidates: list[tuple[str, str, int]] = []  # (raw_url, source, width_hint)
        self.jsonld: list[str] = []
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self._in: str | None = None
        self._is_jsonld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and a.get("href") and not self.base:
            self.base = a["href"]
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if prop in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image",
                        "twitter:image:src", "image", "thumbnailurl"):
                if a.get("content"):
                    self.candidates.append((a["content"], "og", 0))
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "image_src" in rel or (a.get("as") == "image" and "preload" in rel):
                if a.get("href"):
                    self.candidates.append((a["href"], "link", 0))
                if a.get("imagesrcset"):
                    self._srcset(a["imagesrcset"], "srcset")
        elif tag in ("img", "source"):
            width = _int(a.get("width"))
            for key in ("data-original", "data-full", "data-large", "data-zoom-image", "data-hires",
                        "data-highres", "data-src-large", "data-lazy-src", "data-lazy", "data-src",
                        "data-url", "data-image", "src"):
                v = a.get(key)
                if v and not v.startswith("data:"):
                    self.candidates.append((v, "img", width))
            for key in ("srcset", "data-srcset", "data-lazy-srcset"):
                if a.get(key):
                    self._srcset(a[key], "srcset")
        elif tag == "a":
            href = a.get("href", "")
            if _looks_like_image(href):
                self.candidates.append((href, "a", 0))
        if tag == "script":
            self._in = "script"
            self._is_jsonld = "ld+json" in a.get("type", "").lower()
            self._buf = []
        elif tag == "style":
            self._in = "style"
            self._buf = []
        # inline style="background-image:url(...)" and data-bg style attributes
        for key in ("style", "data-bg", "data-background", "data-background-image"):
            v = a.get(key)
            if v:
                for m in CSS_URL.finditer(v):
                    self.candidates.append((m.group(1), "css", 0))
                if key != "style" and "url(" not in v and not v.startswith("data:"):
                    self.candidates.append((v, "css", 0))

    def handle_endtag(self, tag):
        if tag == self._in:
            text = "".join(self._buf)
            if tag == "script":
                (self.jsonld if self._is_jsonld else self.scripts).append(text)
            else:
                self.styles.append(text)
            self._in = None
            self._buf = []

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)

    def _srcset(self, value: str, source: str):
        for url, w in parse_srcset(value):
            self.candidates.append((url, source, w))


def _int(v) -> int:
    try:
        return int(str(v).strip().rstrip("px"))
    except (TypeError, ValueError):
        return 0


def parse_srcset(value: str) -> list[tuple[str, int]]:
    """Parse a srcset into (url, width) pairs. Density descriptors become width*1000 for ranking."""
    out = []
    # URLs may contain commas (e.g. Cloudinary transforms), so split on ", " / ",<space-or-end>"
    for part in re.split(r",\s+(?=\S)", value.strip()):
        bits = part.strip().split()
        if not bits:
            continue
        url, w = bits[0].rstrip(","), 0
        if len(bits) > 1:
            d = bits[1].lower()
            if d.endswith("w"):
                w = _int(d[:-1])
            elif d.endswith("x"):
                try:
                    w = int(float(d[:-1]) * 1000)
                except ValueError:
                    pass
        out.append((url, w))
    return out


def _looks_like_image(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(IMAGE_EXTS)


def _walk_json(obj, out: list[str], key_hint: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            # {"image": {"url": "..."}} -> treat inner url/contentUrl as photo-ish via the parent key
            hint = str(k).lower().replace("_", "")
            if hint in ("url", "contenturl", "src", "href") and key_hint in PHOTO_KEYS:
                hint = key_hint
            _walk_json(v, out, hint)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json(v, out, key_hint)
    elif isinstance(obj, str):
        s = obj.strip()
        if s.startswith(("http://", "https://", "//", "/")) and (
            _looks_like_image(s) or key_hint in PHOTO_KEYS - {"url", "src", "href", "uri"}
        ):
            out.append(s)


def extract_candidates(page_html: str, page_url: str, include_og: bool = True) -> list[Candidate]:
    """Find every plausible listing-photo URL in an HTML document."""
    p = _PageParser()
    p.feed(page_html)
    p.close()
    base = urllib.parse.urljoin(page_url, p.base) if p.base else page_url

    raw: list[tuple[str, str, int]] = []

    # 1. JSON-LD (schema.org RealEstateListing / Residence / Product etc.) — usually the full gallery.
    for block in p.jsonld:
        try:
            data = json.loads(block.strip().rstrip(";"))
        except ValueError:
            continue
        found: list[str] = []
        _walk_json(data, found)
        raw += [(u, "jsonld", 0) for u in found]

    # 2. Markup (<img>, srcset, og:image, <a href=*.jpg>, inline styles).
    raw += [c for c in p.candidates if include_og or c[1] != "og"]

    # 3. Embedded app state (__NEXT_DATA__, window.__INITIAL_STATE__, etc.) and CSS.
    for text in p.scripts + p.styles:
        for m in URL_IN_TEXT.finditer(text):
            raw.append((m.group(0).replace("\\/", "/").replace("\\u002F", "/"), "script", 0))
        for m in CSS_URL.finditer(text):
            raw.append((m.group(1), "css", 0))

    out: list[Candidate] = []
    for i, (u, src, w) in enumerate(raw):
        u = html.unescape(u.strip()).replace("\\u0026", "&").replace("\\/", "/")
        if not u or u.startswith(("data:", "javascript:", "blob:", "#")):
            continue
        absu = urllib.parse.urljoin(base, u)
        if not absu.startswith(("http://", "https://")):
            continue
        out.append(Candidate(absu, src, w, i))
    return out


# --------------------------------------------------------------------------- filtering / dedupe

RESIZE_PARAMS = {"w", "h", "width", "height", "size", "resize", "fit", "crop", "quality", "q",
                 "maxwidth", "maxheight", "mw", "mh", "rw", "rh", "dpr", "thumbnail", "thumb"}


def strip_resize_params(url: str) -> str:
    """Drop common CDN resize query params so the original-size image is requested."""
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() not in RESIZE_PARAMS]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


def photo_key(url: str) -> str:
    """A key that collapses size variants of the same photo (e.g. /640x480/abc.jpg vs /1024x768/abc.jpg,
    abc_thumb.jpg vs abc.jpg, ?w=300 vs ?w=1200)."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path.lower()
    path = re.sub(r"/\d{2,5}x\d{2,5}/", "/", path)                       # /640x480/
    path = re.sub(r"/(?:w|h|s|c|fit|resize)[_\-=]?\d{2,5}[^/]*/", "/", path)  # /w_640,h_480/
    stem, ext = os.path.splitext(path)
    stem = re.sub(r"[_\-.](?:\d{2,5}x\d{2,5}|thumb(?:nail)?|small|medium|large|xl|lg|md|sm|"
                  r"orig(?:inal)?|full|hi(?:gh)?res|\d{2,5}w)$", "", stem)
    # zillowstatic: /fp/<hash>-cc_ft_960.jpg, -uncropped_scaled_within_1536_1152.webp, -p_e.jpg ...
    stem = re.sub(r"(/fp/[0-9a-f]{16,})-[a-z0-9_]+$", r"\1", stem)
    return f"{parts.netloc.lower()}{stem}"


def select_photos(cands: list[Candidate], strip_resize: bool = False) -> list[str]:
    """Filter junk, collapse size variants (keeping the largest), preserve gallery order."""
    best: dict[str, Candidate] = {}
    first_seen: dict[str, int] = {}
    for c in cands:
        url = strip_resize_params(c.url) if strip_resize else c.url
        path = urllib.parse.urlsplit(url).path
        low = path.lower()
        if low.endswith((".svg", ".ico")) or JUNK_PATTERNS.search(path):
            continue
        # Only keep things that look like images: by extension, or found in an image-ish context.
        if not _looks_like_image(url) and c.source not in ("img", "srcset", "og", "jsonld", "link"):
            continue
        k = photo_key(url)
        first_seen.setdefault(k, c.order)
        prev = best.get(k)
        if prev is None or _rank(c, url) > _rank(prev, prev.url):
            best[k] = Candidate(url, c.source, c.width_hint, c.order)
    return [best[k].url for k in sorted(best, key=lambda k: first_seen[k])]


def _rank(c: Candidate, url: str) -> tuple:
    # Prefer explicit large widths, then size hints in the URL, then non-thumbnail names.
    m = re.findall(r"(\d{3,5})x(\d{3,5})", url)
    url_px = max((int(a) * int(b) for a, b in m), default=0)
    url_px = max([url_px] + [int(n) ** 2 for n in re.findall(r"[_-](?:ft|within)_(\d{3,4})", url)])
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    qw = max((_int(q.get(k)) for k in ("w", "width", "maxwidth", "mw")), default=0)
    thumbish = bool(re.search(r"thumb|small|_sm\b|_s\.", url, re.I))
    return (c.width_hint or qw, url_px, not thumbish, -len(url))


# --------------------------------------------------------------------------- image sniffing

def image_info(data: bytes) -> tuple[str | None, int, int]:
    """Return (ext, width, height) by sniffing magic bytes. Width/height 0 if unknown."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg", *_jpeg_size(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return ".png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return ".gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", *_webp_size(data)
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif", 0, 0
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return ".heic", 0, 0
    return None, 0, 0


def _jpeg_size(d: bytes) -> tuple[int, int]:
    i = 2
    while i + 9 < len(d):
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(d[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(d[i + 7:i + 9], "big"), int.from_bytes(d[i + 5:i + 7], "big")
        i += 2 + seg_len
    return 0, 0


def _webp_size(d: bytes) -> tuple[int, int]:
    chunk = d[12:16]
    if chunk == b"VP8 " and len(d) >= 30:
        return int.from_bytes(d[26:28], "little") & 0x3FFF, int.from_bytes(d[28:30], "little") & 0x3FFF
    if chunk == b"VP8L" and len(d) >= 25:
        b = int.from_bytes(d[21:25], "little")
        return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(d) >= 30:
        return int.from_bytes(d[24:27], "little") + 1, int.from_bytes(d[27:30], "little") + 1
    return 0, 0


# --------------------------------------------------------------------------- download

def download_all(urls: list[str], opts: Options, log=print) -> list[Result]:
    os.makedirs(opts.out_dir, exist_ok=True)
    width = max(3, len(str(len(urls))))
    results: list[Result] = [Result(u) for u in urls]
    seen_hashes: dict[str, int] = {}

    def work(idx: int) -> tuple[int, bytes | None, str]:
        url = urls[idx]
        try:
            body, ctype, _ = fetch(url, referer=opts.referer, headers=opts.headers, timeout=opts.timeout,
                                   retries=opts.retries, accept="image/avif,image/webp,image/*,*/*;q=0.8")
            return idx, body, ctype
        except Exception as e:  # noqa: BLE001 - report every failure, keep going
            results[idx].error = f"{type(e).__name__}: {e}"
            return idx, None, ""

    with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
        futures = [pool.submit(work, i) for i in range(len(urls))]
        fetched = {}
        for f in as_completed(futures):
            idx, body, ctype = f.result()
            fetched[idx] = (body, ctype)

    n = 0
    for idx in range(len(urls)):  # write in gallery order so numbering is stable
        body, ctype = fetched[idx]
        r = results[idx]
        if body is None:
            log(f"  ! {urls[idx]}  ({r.error})")
            continue
        ext, w, h = image_info(body)
        if ext is None:
            r.skipped = f"not an image ({ctype or 'unknown type'})"
        elif len(body) < opts.min_bytes:
            r.skipped = f"too small ({len(body)} bytes)"
        elif w and h and max(w, h) < opts.min_dim:
            r.skipped = f"too small ({w}x{h})"
        else:
            digest = hashlib.sha1(body).hexdigest()
            if digest in seen_hashes:
                r.skipped = f"duplicate of #{seen_hashes[digest]}"
            else:
                n += 1
                seen_hashes[digest] = n
                path = os.path.join(opts.out_dir, f"{n:0{width}d}{ext}")
                with open(path, "wb") as fh:
                    fh.write(body)
                r.path, r.size = path, len(body)
                dims = f" {w}x{h}" if w else ""
                log(f"  + {os.path.basename(path)}{dims} {len(body) // 1024}KB  {urls[idx]}")
                continue
        if opts.verbose:
            log(f"  - skipped {urls[idx]}  ({r.skipped})")
    return results


def default_out_dir(page_url: str, listing_id: str | None = None) -> str:
    if listing_id:
        return f"listing-{re.sub(r'[^A-Za-z0-9_-]', '_', listing_id)}"
    parts = urllib.parse.urlsplit(page_url)
    # Prefer a long-ish number in the URL (MLS numbers), else the last path segment.
    nums = re.findall(r"\d{5,}", parts.path + "?" + parts.query)
    if nums:
        return f"listing-{nums[-1]}"
    seg = [s for s in parts.path.split("/") if s]
    name = seg[-1] if seg else parts.netloc
    return re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(name)[0])[:80] or "listing"


def eprint(*a):
    print(*a, file=sys.stderr)
