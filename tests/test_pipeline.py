"""End-to-end run of `python -m tsm_deals` against a fake Blizzard / TSM / Telegram server."""

import json
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

G = 10_000
NOW = datetime.now(timezone.utc).replace(microsecond=0)

REALMS = {
    1: {"realms": [{"slug": "illidan", "name": "Illidan"}], "population": {"type": "FULL"}},
    2: {"realms": [{"slug": "small", "name": "Small"}, {"slug": "tiny", "name": "Tiny"}], "population": {"type": "LOW"}},
    3: {"realms": [{"slug": "bigrealm", "name": "Bigrealm"}], "population": {"type": "FULL"}},
}
AUCTIONS = {
    1: [{"item": {"id": 100}, "buyout": 990 * G, "quantity": 1}],
    2: [
        {"item": {"id": 100}, "buyout": 100 * G, "quantity": 1},  # recipe: deal
        {"item": {"id": 200}, "buyout": 50 * G, "quantity": 1},  # armor: must be dropped
        {"item": {"id": 300}, "buyout": 10 * G, "quantity": 1},  # commodity id: dropped
        {"item": {"id": 400}, "buyout": 2000 * G, "quantity": 1},  # over 1000g cap
        {"item": {"id": 82800, "pet_species_id": 3530}, "buyout": 200 * G, "quantity": 1},  # pet: deal
    ],
    3: [],
}
ITEMS = {100: (9, "Recipe", "Recipe: Test Feast"), 200: (4, "Armor", "Shiny Helm"), 400: (15, "Miscellaneous", "Toy")}
REGION_ITEMS = (
    "itemId,name,marketValue,historical,avgSalePrice,saleRate,soldPerDay,updatedAt\n"
    f"100,Recipe: Test Feast,{1000 * G},0,{1000 * G},0.3,2,2026-11-10T00:00:00Z\n"
    f"200,Shiny Helm,{1000 * G},0,{1000 * G},0.3,2,2026-11-10T00:00:00Z\n"
    f"300,Ore,{1000 * G},0,{1000 * G},0.3,2,2026-11-10T00:00:00Z\n"
    f"400,Toy,{20000 * G},0,{20000 * G},0.3,2,2026-11-10T00:00:00Z\n"
)
REGION_PETS = (
    "petSpeciesId,name,marketValue,historical,avgSalePrice,saleRate,soldPerDay,updatedAt\n"
    f"3530,Miloh,{2000 * G},0,{2000 * G},0.2,1,2026-11-10T00:00:00Z\n"
)
COMMODITIES = "itemId,name,marketValue,minBuyout,recent,historical,updatedAt\n300,Ore,1,1,1,1,2026-11-10T00:00:00Z\n"


class Fake(BaseHTTPRequestHandler):
    sent: list = []
    auction_calls: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/token":
            return self._send(200, b'{"access_token":"tok"}')
        if "/sendMessage" in self.path:
            Fake.sent.append(json.loads(body))
            return self._send(200, b'{"ok":true}')
        self._send(404)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/data/"):
            assert self.headers["Authorization"] == "Bearer tok"
        if path == "/data/wow/connected-realm/index":
            hrefs = [{"href": f"http://x/data/wow/connected-realm/{i}?namespace=dynamic-us"} for i in REALMS]
            return self._send(200, json.dumps({"connected_realms": hrefs}).encode())
        m = re.fullmatch(r"/data/wow/connected-realm/(\d+)(/auctions)?", path)
        if m:
            cid = int(m.group(1))
            if not m.group(2):
                return self._send(200, json.dumps({"id": cid, **REALMS[cid]}).encode())
            Fake.auction_calls.append(cid)
            ims = self.headers.get("If-Modified-Since")
            if ims and parsedate_to_datetime(ims) >= NOW:
                return self._send(304)
            body = json.dumps({"auctions": AUCTIONS[cid]}).encode()
            return self._send(200, body, headers={"Last-Modified": format_datetime(NOW, usegmt=True)})
        m = re.fullmatch(r"/data/wow/item/(\d+)", path)
        if m:
            cls, cname, name = ITEMS[int(m.group(1))]
            return self._send(200, json.dumps({"name": name, "item_class": {"id": cls, "name": cname}}).encode())
        csvs = {"/us/region/items.csv": REGION_ITEMS, "/us/region/pets.csv": REGION_PETS, "/us/commodities.csv": COMMODITIES}
        if path in csvs:
            return self._send(200, csvs[path].encode(), "text/csv")
        self._send(404)


class Pipeline(unittest.TestCase):
    def test_end_to_end(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        env = {
            "BLIZZARD_CLIENT_ID": "id", "BLIZZARD_CLIENT_SECRET": "secret",
            "BLIZZARD_API_URL": base, "BLIZZARD_OAUTH_URL": f"{base}/token",
            "TELEGRAM_BOT_TOKEN": "bot", "TELEGRAM_CHAT_ID": "42", "TELEGRAM_API_URL": base,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            import tsm_deals.data as data

            with mock.patch.object(data, "TSM_BASE_URL", base), mock.patch.object(data, "POLITE_DELAY", 0), \
                    mock.patch("tsm_deals.report.time.sleep"):
                from tsm_deals.__main__ import main

                cfg = Path(tmp) / "config.json"
                cfg.write_text(json.dumps({"sell_realms": ["illidan"]}))
                state = Path(tmp) / "state"
                self.assertEqual(main(["--config", str(cfg), "--state", str(state)]), 0)

                self.assertEqual(len(Fake.sent), 1)
                text = Fake.sent[0]["text"]
                self.assertIn("Recipe: Test Feast", text)
                self.assertIn("BUY <b>Small / Tiny for 100g", text)
                self.assertIn("SELL <b>Illidan at", text)
                self.assertIn("Miloh", text)
                self.assertNotIn("Shiny Helm", text)  # armor excluded
                self.assertNotIn("Ore", text)  # commodity excluded
                self.assertNotIn("Toy", text)  # over the 1000g cap
                self.assertEqual(Fake.sent[0]["chat_id"], "42")

                # Second run: auctions unchanged (304), deals already sent -> no new message.
                Fake.auction_calls.clear()
                self.assertEqual(main(["--config", str(cfg), "--state", str(state)]), 0)
                self.assertEqual(len(Fake.sent), 1)
                self.assertEqual(sorted(Fake.auction_calls), [1, 2, 3])
        server.shutdown()


if __name__ == "__main__":
    unittest.main()
