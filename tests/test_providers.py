import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mls_grab import cli, providers  # noqa: E402
from mls_grab.providers import base, realtor, redfin, zillow  # noqa: E402
from mls_grab.providers.base import ProviderError, address_score  # noqa: E402

ADDR = "1 Tribou Drive, Paris, ME 04281"

REDFIN_AUTOCOMPLETE = {"payload": {"exactMatch": {
    "id": "1_9999", "type": "1", "name": "1 Tribou Dr", "subName": "Paris, ME 04281",
    "url": "/ME/Paris/1-Tribou-Dr-04281/home/9999"}, "sections": [{"rows": [
        {"name": "Paris", "subName": "ME", "url": "/city/123/ME/Paris"},
        {"name": "11 Tribou Dr", "subName": "Paris, ME 04281", "url": "/ME/Paris/11-Tribou-Dr-04281/home/1111"},
    ]}]}}
REDFIN_INITIAL = {"payload": {"propertyId": 9999, "listingId": 5555}}
REDFIN_ATF = {"payload": {
    "addressSectionInfo": {"status": {"displayValue": "Active"}},
    "mediaBrowserInfo": {"photos": [
        {"photoUrls": {"fullScreenPhotoUrl": f"https://ssl.cdn-redfin.com/photo/1/bigphoto/368/1683368_{i}_0.jpg",
                       "nonFullScreenPhotoUrl": f"https://ssl.cdn-redfin.com/photo/1/mbphoto/368/1683368_{i}_0.jpg"}}
        for i in range(3)]}}}

REALTOR_SUGGEST = {"autocomplete": [
    {"mpr_id": "7777", "full_address": ["1 Tribou Dr, Paris, ME, 04281"]},
    {"mpr_id": "8888", "full_address": ["1 Tribou Rd, Other, NH, 03000"]},
]}
REALTOR_GQL = {"data": {"home": {"property_id": "7777", "status": "for_sale",
                                 "href": "https://www.realtor.com/realestateandhomes-detail/1-Tribou-Dr_Paris_ME_04281_M7777",
                                 "photos": [{"href": f"https://ap.rdcpix.com/abc{i}l-m{i}s.jpg"} for i in range(4)]}}}


def fake_json(table):
    def _get_json(url, **kw):
        for frag, resp in table.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected URL {url}")
    return _get_json


class AddressMatching(unittest.TestCase):
    def test_scores(self):
        self.assertGreater(address_score(ADDR, "1 Tribou Dr, Paris, ME 04281"), 0.9)
        self.assertEqual(address_score(ADDR, "11 Tribou Dr, Paris, ME 04281"), 0.0)   # house number
        self.assertEqual(address_score(ADDR, "1 Tribou Dr, Paris, ME 04282"), 0.0)    # zip
        self.assertLess(address_score(ADDR, "1 Main St, Boston, MA"), 0.6)

    def test_cli_address_detection(self):
        self.assertTrue(cli.looks_like_address(ADDR))
        self.assertTrue(cli.looks_like_address("12B Baker Street London"))
        self.assertFalse(cli.looks_like_address("1683368"))
        self.assertFalse(cli.looks_like_address("Paris ME"))

    def test_blocked_detection(self):
        self.assertTrue(base.looks_blocked("<div id='px-captcha'></div>"))
        self.assertFalse(base.looks_blocked("<html><body>home</body></html>"))


class Redfin(unittest.TestCase):
    def test_lookup_picks_exact_home_and_fullscreen_photos(self):
        with mock.patch.object(redfin, "get_json", fake_json({
                "location-autocomplete": REDFIN_AUTOCOMPLETE,
                "initialInfo": REDFIN_INITIAL, "aboveTheFold": REDFIN_ATF})):
            found = redfin.lookup(ADDR, 5)
        self.assertEqual(found.match.url, "https://www.redfin.com/ME/Paris/1-Tribou-Dr-04281/home/9999")
        self.assertEqual(len(found.photos), 3)
        self.assertTrue(all("/bigphoto/" in u for u in found.photos))
        self.assertEqual(found.status, "Active")

    def test_not_found(self):
        empty = {"payload": {"sections": []}}
        with mock.patch.object(redfin, "get_json", fake_json({"location-autocomplete": empty})):
            with self.assertRaises(ProviderError) as cm:
                redfin.lookup(ADDR, 5)
        self.assertEqual(cm.exception.kind, "not_found")

    def test_falls_back_to_home_page(self):
        page = ('<html><img src="https://ssl.cdn-redfin.com/photo/1/bigphoto/368/1683368_0_0.jpg">'
                '<img src="https://ssl.cdn-redfin.com/logos/redfin-logo.png"></html>')
        with mock.patch.object(redfin, "get_json", fake_json({
                "location-autocomplete": REDFIN_AUTOCOMPLETE,
                "initialInfo": ProviderError("error", "api changed")})), \
                mock.patch.object(redfin, "get", return_value=page.encode()):
            found = redfin.lookup(ADDR, 5)
        self.assertEqual(found.photos, ["https://ssl.cdn-redfin.com/photo/1/bigphoto/368/1683368_0_0.jpg"])


class Realtor(unittest.TestCase):
    def test_lookup_large_photos(self):
        with mock.patch.object(realtor, "get_json", fake_json({"suggest": REALTOR_SUGGEST})), \
                mock.patch.object(realtor, "get", return_value=json.dumps(REALTOR_GQL).encode()) as g:
            found = realtor.lookup(ADDR, 5)
        sent = json.loads(g.call_args.kwargs["data"])
        self.assertEqual(sent["variables"]["property_id"], "7777")
        self.assertEqual(found.photos[0], "https://ap.rdcpix.com/abc0l-m0rd-w2048_h1536.jpg")
        self.assertEqual(len(found.photos), 4)
        self.assertIn("_M7777", found.match.url)


class Chain(unittest.TestCase):
    def test_falls_through_blocked_providers(self):
        blocked = mock.Mock(side_effect=ProviderError("blocked", "captcha"))
        ok = providers.Found(base.Match("realtor", "1 Tribou Dr, Paris, ME 04281", "https://x"), ["https://p/1.jpg"])
        with mock.patch.object(redfin, "lookup", blocked), \
                mock.patch.object(realtor, "lookup", return_value=ok), \
                mock.patch.object(zillow, "lookup") as z:
            found, attempts = providers.find_photos(ADDR)
        self.assertIs(found, ok)
        self.assertEqual([a[:2] for a in attempts], [("redfin", "blocked"), ("realtor", "ok")])
        z.assert_not_called()

    def test_cli_json_summary_when_all_fail(self):
        err = mock.Mock(side_effect=ProviderError("blocked", "captcha"))
        with mock.patch.object(redfin, "lookup", err), mock.patch.object(realtor, "lookup", err), \
                mock.patch.object(zillow, "lookup", err):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([ADDR, "--json"])
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["attempts"]), 3)
        self.assertIn("blocked this server", out["error"])

    def test_cli_address_download_and_zip(self):
        from tests.test_mls_grab import make_jpeg  # reuse image generator
        found = providers.Found(base.Match("redfin", "1 Tribou Dr, Paris, ME 04281", "https://www.redfin.com/x"),
                                [f"https://ssl.cdn-redfin.com/{i}.jpg" for i in range(3)], "Active")

        class Resp(io.BytesIO):
            headers = {"Content-Type": "image/jpeg"}

            def geturl(self):
                return ""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            return Resp(make_jpeg(1024, 768, req.full_url))

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(providers, "find_photos", return_value=(found, [("redfin", "ok", "")])), \
                mock.patch("urllib.request.urlopen", urlopen):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([ADDR, "-o", os.path.join(d, "o"), "--zip", "--json"])
            out = json.loads(buf.getvalue())
            self.assertEqual(rc, 0, out)
            self.assertEqual(out["saved"], 3)
            self.assertEqual(out["provider"], "redfin")
            self.assertTrue(os.path.exists(out["zip"]))
            import zipfile
            self.assertEqual(sorted(zipfile.ZipFile(out["zip"]).namelist()),
                             ["001.jpg", "002.jpg", "003.jpg", "sources.json"])


if __name__ == "__main__":
    unittest.main()
