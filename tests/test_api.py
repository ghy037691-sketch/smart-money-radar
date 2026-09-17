"""Integration tests: boot the real HTTP server with a pre-built synthetic
cache (no network) and verify the public API surface."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import names  # noqa: E402
import test_thirteenf as tf  # noqa: E402
import thirteenf  # noqa: E402

CACHE_DIR = tempfile.mkdtemp()
os.environ["CACHE_PATH"] = os.path.join(CACHE_DIR, "cache.json")
sys.path.insert(0, ROOT)
import server  # noqa: E402

server.CACHE_PATH = os.path.join(CACHE_DIR, "cache.json")


def _seed_state():
    def pos(issuer, cusip, value, shares, issuance, cls="COM"):
        return {"issuer": issuer, "class": cls, "cusip": cusip, "value_usd": value,
                "shares": shares, "put_call": None, "issuance": issuance}
    q1a = tf.mk_quarter(tf.XML_Q1, "2026-03-31", name="ALPHA FUND LP", cik=1)
    q2a = tf.mk_quarter(tf.XML_Q2, "2026-06-30", name="ALPHA FUND LP", cik=1)
    q1b = tf.mk_quarter(b"""<informationTable><submitter><valueTotal>10000</valueTotal></submitter>
      <infoTable><nameOfIssuer>GAMMA LLC</nameOfIssuer><titleOfClass>COM</titleOfClass>
      <cusip>010203040</cusip><value>10000</value><sshPrnamt>40000</sshPrnamt>
      <issuanceType>Decrease</issuanceType></infoTable></informationTable>""",
      "2026-03-31", name="BETA FUND LP", cik=2)
    q2b = tf.mk_quarter(b"""<informationTable><submitter><valueTotal>20000</valueTotal></submitter>
      <infoTable><nameOfIssuer>GAMMA LLC</nameOfIssuer><titleOfClass>COM</titleOfClass>
      <cusip>010203040</cusip><value>20000</value><sshPrnamt>80000</sshPrnamt>
      <issuanceType>Increase</issuanceType></infoTable></informationTable>""",
      "2026-06-30", name="BETA FUND LP", cik=2)
    qoq_a = thirteenf.fund_qoq(q2a, q1a)
    qoq_b = thirteenf.fund_qoq(q2b, q1b)
    qoq_a["all_positions"] = [
        {"issuer": p["issuer"], "class": p["class"], "cusip": p["cusip"],
         "value_usd": p["value_usd"], "shares": p["shares"], "put_call": p["put_call"],
         "issuance": p["issuance"], "ticker": None}
        for p in q2a["positions"].values()
    ]
    qoq_b["all_positions"] = [
        {"issuer": p["issuer"], "class": p["class"], "cusip": p["cusip"],
         "value_usd": p["value_usd"], "shares": p["shares"], "put_call": p["put_call"],
         "issuance": p["issuance"], "ticker": None}
        for p in q2b["positions"].values()
    ]
    qoq_a["top_positions"] = qoq_a["all_positions"][:3]
    qoq_b["top_positions"] = qoq_b["all_positions"][:3]
    reports = [
        {"fund": tf.FUND_A, "qoq": qoq_a},
        {"fund": tf.FUND_B, "qoq": qoq_b},
    ]
    funds = []
    for r in reports:
        q = r["qoq"]
        funds.append({
            "slug": r["fund"]["slug"], "name": r["fund"]["name"], "manager": r["fund"].get("manager"),
            "cik": r["fund"]["cik"], "period": q["period"],
            "total_value_usd": q["total_value_usd"], "positions_count": q["positions_count"],
            "new_count": len(q["new_positions"]), "exited_count": len(q["exited_positions"]),
            "error": None,
        })
    moves = thirteenf.top_moves(reports, min_funds=2, include_etfs=False, limit=50)
    with server._state["lock"]:
        server._state.update({
            "status": "ready",
            "generated_at": "2026-09-17T00:00:00+00:00",
            "funds": funds,
            "qoq": {r["fund"]["slug"]: r["qoq"] for r in reports},
            "moves": moves,
            "errors": {},
        })
    # offline ticker index (no network)
    names._cache.update({
        "at": time.time(),
        "index": {
            "GAMMA LLC": ("GAM", "Gamma Llc"),
            "ALPHA CORP": ("AAA", "Alpha Corp"),
            "BETA INC": ("BEE", "Beta Inc"),
        },
    })


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed_state()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=15) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_health(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["cache_status"], "ready")

    def test_funds(self):
        status, body = self._get("/api/funds")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["funds"]), 2)
        self.assertEqual(body["funds"][0]["period"], "2026-06-30")

    def test_moves(self):
        status, body = self._get("/api/moves?limit=10")
        self.assertEqual(status, 200)
        buyers = {r["issuer"]: r for r in body["buys"]}
        sellers = {r["issuer"]: r for r in body["sells"]}
        # Fund A: ALPHA +20M, new GAMMA +55M, exit BETA -35M, ETF +10M.
        # Fund B: GAMMA +10M. => GAMMA net +65M convergence; BETA -35M sell.
        self.assertIn("GAMMA LLC", buyers)
        self.assertTrue(buyers["GAMMA LLC"]["convergence"])
        self.assertEqual(buyers["GAMMA LLC"]["net_delta_usd"], 65_000_000)
        self.assertIn("ALPHA CORP", buyers)
        self.assertNotIn("VANGUARD S&P 500 ETF", buyers)  # excluded by default
        self.assertIn("BETA INC", sellers)

    def test_fund_detail_hides_all_positions(self):
        status, body = self._get("/api/fund?slug=alpha-fund")
        self.assertEqual(status, 200)
        self.assertEqual(body["period"], "2026-06-30")
        self.assertIn("top_positions", body)
        self.assertNotIn("all_positions", body)

    def test_stock_lookup(self):
        status, body = self._get("/api/stock?q=GAM")
        self.assertEqual(status, 200)
        # both funds hold GAMMA this quarter; sorted by value desc
        self.assertEqual(body["funds_holding"], 2)
        self.assertEqual(body["holdings"][0]["fund"], "ALPHA FUND LP")
        self.assertEqual(body["holdings"][0]["ticker"], "GAM")

    def test_stock_lookup_by_cusip(self):
        status, body = self._get("/api/stock?q=001234101")
        self.assertEqual(status, 200)
        self.assertEqual(body["funds_holding"], 1)

    def test_stock_lookup_missing(self):
        status, body = self._get("/api/stock?q=ZZZZ")
        self.assertEqual(status, 200)
        self.assertEqual(body["funds_holding"], 0)

    def test_insider(self):
        fake = {"window": {"start": "2026-08-18", "end": "2026-09-17"}, "filings_scanned": 1,
                "matched_total": 5, "min_value_usd": 100000, "returned": 1, "clusters_found": 0,
                "insider_buys": [{"company": "TEST CO", "ticker": "TST", "cik": 1,
                                  "insider": "Jane Doe", "roles": ["Officer"], "date": "2026-09-01",
                                  "shares": 10000, "price_per_share": 50, "value_usd": 500000,
                                  "filed": "2026-09-02", "source_url": "http://example",
                                  "cluster": False, "signal": "insider buy"}]}
        original = thirteenf.insider_buys
        thirteenf.insider_buys = lambda **kw: fake
        try:
            status, body = self._get("/api/insider?days=30")
            self.assertEqual(status, 200)
            self.assertEqual(body["returned"], 1)
            self.assertEqual(body["insider_buys"][0]["ticker"], "TST")
        finally:
            thirteenf.insider_buys = original

    def test_404(self):
        status, _ = self._get("/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
