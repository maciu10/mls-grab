import os
import random
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mls_grab import cli  # noqa: E402
from mls_grab.core import photo_key  # noqa: E402
from mls_grab.sites import for_url, zillow  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
URL = "https://www.zillow.com/homedetails/1-Tribou-Drive-Paris-ME-04281/304949881_zpid/"


class ZillowAdapterTests(unittest.TestCase):
    def test_matches_and_builds_url(self):
        self.assertIs(for_url(URL), zillow)
        self.assertIsNone(for_url("https://example.com/x"))
        self.assertEqual(zillow.listing_url("304949881"), "https://www.zillow.com/homedetails/304949881_zpid/")
        self.assertEqual(zillow.listing_url("304949881_zpid"), "https://www.zillow.com/homedetails/304949881_zpid/")

    def test_extracts_only_this_home_largest_jpeg(self):
        photos = zillow.extract((FIX / "zillow.html").read_text(), URL)
        self.assertEqual(len(photos), 5)
        self.assertTrue(all(p.endswith("-cc_ft_1536.jpg") for p in photos), photos)
        self.assertTrue(photos[0].startswith("https://photos.zillowstatic.com/fp/01"))
        self.assertFalse(any("ffffeeee" in p for p in photos))  # nearby home excluded

    def test_without_zpid_picks_largest_gallery(self):
        photos = zillow.extract((FIX / "zillow.html").read_text(), "file:///tmp/saved.html")
        self.assertEqual(len(photos), 5)

    def test_captcha_detected(self):
        self.assertIsNotNone(zillow.blocked((FIX / "zillow_captcha.html").read_text()))
        self.assertIsNone(zillow.blocked((FIX / "zillow.html").read_text()))

    def test_generic_key_collapses_zillow_sizes(self):
        a = "https://photos.zillowstatic.com/fp/0123456789abcdef0123-cc_ft_384.jpg"
        b = "https://photos.zillowstatic.com/fp/0123456789abcdef0123-uncropped_scaled_within_1536_1152.jpg"
        self.assertEqual(photo_key(a), photo_key(b))

    def test_cli_captcha_exit(self):
        rc = cli.main(["--html", str(FIX / "zillow_captcha.html"), "--base-url", URL, "-l"])
        self.assertEqual(rc, 1)


class ZillowEndToEnd(unittest.TestCase):
    def test_download_saved_page(self):
        def jpeg(seed):
            filler = bytes(random.Random(seed).getrandbits(8) for _ in range(30000)).replace(b"\xff", b"\0")
            return b"\xff\xd8\xff\xc0\x00\x0b\x08\x04\x80\x06\x00\x01\x01\x11\x00\xff\xda" + filler + b"\xff\xd9"

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = jpeg(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                page = Path(d, "z.html")
                page.write_text((FIX / "zillow.html").read_text().replace(
                    "https://photos.zillowstatic.com", f"http://127.0.0.1:{srv.server_port}"))
                out = os.path.join(d, "out")
                self.assertEqual(cli.main(["--html", str(page), "--base-url", URL, "-o", out]), 0)
                files = sorted(f for f in os.listdir(out) if f.endswith(".jpg"))
                self.assertEqual(files, ["001.jpg", "002.jpg", "003.jpg", "004.jpg", "005.jpg"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
