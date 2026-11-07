import json
import os
import random
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mls_grab import cli  # noqa: E402
from mls_grab.core import (extract_candidates, image_info, parse_srcset, photo_key,  # noqa: E402
                           select_photos, strip_resize_params)

FIXTURE = Path(__file__).parent / "fixtures" / "listing.html"
PAGE = "https://mls.example.test/listing/1683368"


def make_png(w, h, seed):
    rnd = random.Random(seed)
    raw = b"".join(b"\x00" + bytes(rnd.getrandbits(8) for _ in range(w * 3)) for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b""))


def make_jpeg(w, h, seed, pad=40_000):
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    filler = bytes(random.Random(seed).getrandbits(8) for _ in range(pad)).replace(b"\xff", b"\x00")
    return b"\xff\xd8" + sof + b"\xff\xda\x00\x02" + filler + b"\xff\xd9"


def make_webp(w, h, pad=40_000):
    vp8x = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") \
        + (h - 1).to_bytes(3, "little")
    body = b"WEBP" + vp8x + b"\x00" * pad
    return b"RIFF" + struct.pack("<I", len(body)) + body


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.photos = select_photos(extract_candidates(FIXTURE.read_text(), PAGE))

    def test_finds_gallery_in_order_largest_variant(self):
        base = "https://mls.example.test/media/1683368"
        gallery = [p for p in self.photos if "1683368" in p and "yard" not in p]
        self.assertEqual(gallery[:5], [
            f"{base}/01/1024x768/front.jpg",
            f"{base}/02/1024x768/kitchen.jpg",
            f"{base}/03/1024x768/living.jpg",
            f"{base}/05/1024x768/bath.jpg",
            "https://cdn.example.test/1683368/06.webp?w=1600",  # from embedded JS state
        ])
        self.assertIn(f"{base}/04/1024x768/yard.jpg", self.photos)  # from CSS background
        self.assertNotIn("https://cdn.example.test/1683368/06.webp?w=400", self.photos)

    def test_filters_junk(self):
        joined = "\n".join(self.photos)
        for junk in ("logo", "spinner", "favicon", "headshot", "data:"):
            self.assertNotIn(junk, joined)

    def test_srcset(self):
        self.assertEqual(parse_srcset("a.jpg 320w, b.jpg 1024w"), [("a.jpg", 320), ("b.jpg", 1024)])
        self.assertEqual(parse_srcset("a.jpg 1x, b.jpg 2x"), [("a.jpg", 1000), ("b.jpg", 2000)])

    def test_photo_key_collapses_sizes_not_photos(self):
        self.assertEqual(photo_key("https://x/p/640x480/a.jpg"), photo_key("https://x/p/1024x768/a.jpg"))
        self.assertEqual(photo_key("https://x/a_thumb.jpg"), photo_key("https://x/a.jpg"))
        self.assertNotEqual(photo_key("https://x/photo-1.jpg"), photo_key("https://x/photo-2.jpg"))
        self.assertNotEqual(photo_key("https://x/a-b.jpg"), photo_key("https://x/a-c.jpg"))

    def test_strip_resize(self):
        self.assertEqual(strip_resize_params("https://x/a.jpg?w=300&h=200&id=5"), "https://x/a.jpg?id=5")

    def test_image_info(self):
        self.assertEqual(image_info(make_png(40, 30, 1)), (".png", 40, 30))
        self.assertEqual(image_info(make_jpeg(1024, 768, 1, pad=10)), (".jpg", 1024, 768))
        self.assertEqual(image_info(make_webp(1600, 1200, pad=10)), (".webp", 1600, 1200))
        self.assertEqual(image_info(b"<html>"), (None, 0, 0))


class EndToEndTests(unittest.TestCase):
    """Serve the fixture + images from a local HTTP server and run the real CLI against it."""

    @classmethod
    def setUpClass(cls):
        same = make_jpeg(1024, 768, 3)
        cls.files = {
            "/media/1683368/01/1024x768/front.jpg": (make_jpeg(1024, 768, 1), "image/jpeg"),
            "/media/1683368/02/1024x768/kitchen.jpg": (make_png(400, 300, 2), "image/png"),
            "/media/1683368/03/1024x768/living.jpg": (same, "image/jpeg"),
            "/media/1683368/04/1024x768/yard.jpg": (same, "image/jpeg"),  # byte-identical dup
            "/media/1683368/05/1024x768/bath.jpg": (make_jpeg(1024, 768, 5), "image/jpeg"),
            "/1683368/06.webp": (make_webp(1600, 1200), "image/webp"),
            "/media/tiny.jpg": (make_jpeg(100, 80, 9), "image/jpeg"),
            "/media/notimage.jpg": (b"<html>nope</html>" * 2000, "text/html"),
        }
        files = cls.files

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                if path not in files:
                    self.send_error(404)
                    return
                body, ctype = files[path]
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.url = f"{origin}/listing/1683368"
        # Point the "CDN" photo at the local server too (JSON-escaped, as in real embedded state).
        html = FIXTURE.read_text().replace("https:\\/\\/cdn.example.test", origin.replace("/", "\\/"))
        files["/listing/1683368"] = (html.encode(), "text/html")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_download_by_url(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            rc = cli.main([self.url, "-o", out])
            self.assertEqual(rc, 0)
            names = sorted(os.listdir(out))
            self.assertEqual(names, ["001.jpg", "002.png", "003.jpg", "004.jpg", "005.webp", "sources.json"])
            src = json.load(open(os.path.join(out, "sources.json")))
            self.assertTrue(src["photos"][0]["url"].endswith("/01/1024x768/front.jpg"))
            self.assertTrue(src["photos"][3]["url"].endswith("/05/1024x768/bath.jpg"))

    def test_listing_id_with_template(self):
        tmpl = self.url.replace("1683368", "{id}")
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(cli.main(["1683368", "-t", tmpl]), 0)
                self.assertTrue(os.path.isdir("listing-1683368"))
                self.assertEqual(len([f for f in os.listdir("listing-1683368") if f[0].isdigit()]), 5)
            finally:
                os.chdir(cwd)

    def test_saved_html(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            rc = cli.main(["--html", str(FIXTURE), "--base-url", self.url, "-o", out, "--no-og", "--retries", "1"])
            self.assertEqual(rc, 0)
            self.assertGreaterEqual(len(os.listdir(out)), 5)


if __name__ == "__main__":
    unittest.main()
