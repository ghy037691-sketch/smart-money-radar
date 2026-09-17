"""Engine tests: 13F parsing, QoQ diff, cross-fund convergence, ETF filter."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import thirteenf  # noqa: E402

XML_Q1 = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable>
  <version>XML 1.0</version>
  <submitter>
    <nameOfReportManager>TEST FUND</nameOfReportManager>
    <tableEntryTotal>3</tableEntryTotal>
    <valueTotal>100000</valueTotal>
  </submitter>
  <infoTable>
    <nameOfIssuer>ALPHA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>001234101</cusip>
    <value>40000</value>
    <sshPrnamt>100000</sshPrnamt>
    <putCall/>
    <issuanceType>Decrease</issuanceType>
  </infoTable>
  <infoTable>
    <nameOfIssuer>BETA INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>009876543</cusip>
    <value>35000</value>
    <sshPrnamt>50000</sshPrnamt>
    <issuanceType>Initial</issuanceType>
  </infoTable>
  <infoTable>
    <nameOfIssuer>VANGUARD S&amp;P 500 ETF</nameOfIssuer>
    <titleOfClass>INDEX</titleOfClass>
    <cusip>01628V103</cusip>
    <value>25000</value>
    <sshPrnamt>900</sshPrnamt>
    <issuanceType>Decrease</issuanceType>
  </infoTable>
</informationTable>
"""

XML_Q2 = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable>
  <version>XML 1.0</version>
  <submitter>
    <nameOfReportManager>TEST FUND</nameOfReportManager>
    <tableEntryTotal>3</tableEntryTotal>
    <valueTotal>150000</valueTotal>
  </submitter>
  <infoTable>
    <nameOfIssuer>ALPHA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>001234101</cusip>
    <value>60000</value>
    <sshPrnamt>150000</sshPrnamt>
    <issuanceType>Increase</issuanceType>
  </infoTable>
  <infoTable>
    <nameOfIssuer>GAMMA LLC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>010203040</cusip>
    <value>55000</value>
    <sshPrnamt>200000</sshPrnamt>
    <issuanceType>Initial</issuanceType>
  </infoTable>
  <infoTable>
    <nameOfIssuer>VANGUARD S&amp;P 500 ETF</nameOfIssuer>
    <titleOfClass>INDEX</titleOfClass>
    <cusip>01628V103</cusip>
    <value>35000</value>
    <sshPrnamt>1300</sshPrnamt>
    <issuanceType>Decrease</issuanceType>
  </infoTable>
</informationTable>
"""

# Modern namespace format: whole-dollar values, sub-manager rows to aggregate,
# no putCall/issuanceType.
XML_MODERN = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>ALLY FINL INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>02005N100</cusip>
    <value>100000000</value>
    <shrsOrPrnAmt><sshPrnamt>1000000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>ALLY FINL INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>02005N100</cusip>
    <value>50000000</value>
    <shrsOrPrnAmt><sshPrnamt>500000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>GAMMA LLC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>010203040</cusip>
    <value>25000000</value>
    <shrsOrPrnAmt><sshPrnamt>250000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>
"""

FUND_A = {"slug": "alpha-fund", "name": "ALPHA FUND LP", "manager": "A", "cik": 1}
FUND_B = {"slug": "beta-fund", "name": "BETA FUND LP", "manager": "B", "cik": 2}


def mk_quarter(quarter_xml, period, name="TEST FUND", cik=99):
    positions, total = thirteenf.parse_13f_xml(quarter_xml)
    return {"cik": cik, "name": name, "period": period, "filed": period,
            "accession": "x", "total_value_usd": total, "positions": positions,
            "source_url": "http://example"}


class ParseTests(unittest.TestCase):
    def test_parse_positions_and_thousands(self):
        positions, total = thirteenf.parse_13f_xml(XML_Q1)
        self.assertEqual(total, 100_000_000)  # thousands -> USD
        self.assertEqual(len(positions), 3)
        a = positions["001234101"]
        self.assertEqual(a["issuer"], "ALPHA CORP")
        self.assertEqual(a["value_usd"], 40_000_000)
        self.assertEqual(a["shares"], 100000)
        self.assertIsNone(a["put_call"])
        self.assertEqual(a["issuance"], "Decrease")

    def test_qoq_diff(self):
        q1 = mk_quarter(XML_Q1, "2026-03-31")
        q2 = mk_quarter(XML_Q2, "2026-06-30")
        qoq = thirteenf.fund_qoq(q2, q1)
        self.assertEqual(qoq["period"], "2026-06-30")
        self.assertEqual(qoq["prev_period"], "2026-03-31")
        self.assertEqual([p["issuer"] for p in qoq["new_positions"]], ["GAMMA LLC"])
        self.assertEqual([p["issuer"] for p in qoq["exited_positions"]], ["BETA INC"])
        # increased sorted by delta desc: ALPHA +20M, then the ETF +10M
        self.assertEqual(qoq["increased"][0]["issuer"], "ALPHA CORP")
        self.assertEqual(qoq["increased"][0]["delta_usd"], 20_000_000)
        self.assertEqual(qoq["increased"][1]["issuer"], "VANGUARD S&P 500 ETF")
        self.assertEqual(qoq["increased"][1]["delta_usd"], 10_000_000)
        self.assertEqual(qoq["decreased"], [])

    def test_modern_format_whole_dollars_and_aggregation(self):
        positions, total = thirteenf.parse_13f_xml(XML_MODERN)
        self.assertEqual(total, 175_000_000)  # whole dollars, no x1000
        # sub-manager rows for the same CUSIP are aggregated
        self.assertEqual(len(positions), 2)
        ally = positions["02005N100"]
        self.assertEqual(ally["value_usd"], 150_000_000)
        self.assertEqual(ally["shares"], 1_500_000)
        self.assertEqual(ally["issuer"], "ALLY FINL INC")
        gamma = positions["010203040"]
        self.assertEqual(gamma["value_usd"], 25_000_000)
        self.assertIsNone(gamma["put_call"])
        self.assertIsNone(gamma["issuance"])

    def test_etf_heuristic(self):
        self.assertTrue(thirteenf._looks_like_etf("VANGUARD S&P 500 ETF", "INDEX"))
        self.assertTrue(thirteenf._looks_like_etf("ISHARES CORE S&P 100", "INDEX"))
        self.assertTrue(thirteenf._looks_like_etf("SPDR PORTFOLIO S&P 500", ""))
        self.assertFalse(thirteenf._looks_like_etf("APPLE INC", "COM"))
        self.assertFalse(thirteenf._looks_like_etf("NVIDIA CORP", "COM"))


class MovesTests(unittest.TestCase):
    def setUp(self):
        # no network in unit tests: ticker resolution off (keys stay CUSIP-based)
        import names
        self._orig_ticker_for = names.ticker_for
        names.ticker_for = lambda issuer: None

    def tearDown(self):
        import names
        names.ticker_for = self._orig_ticker_for

    def _reports(self):
        def pos(issuer, cusip, value, shares, issuance, cls="COM"):
            return {"issuer": issuer, "class": cls, "cusip": cusip, "value_usd": value,
                    "shares": shares, "put_call": None, "issuance": issuance}
        # Fund A: buys ALPHA +20M, new GAMMA +55M, exits BETA -35M, ETF +10M
        q1a = mk_quarter(XML_Q1, "2026-03-31", name="ALPHA FUND LP", cik=1)
        q2a = mk_quarter(XML_Q2, "2026-06-30", name="ALPHA FUND LP", cik=1)
        # Fund B: only GAMMA, increased 10M -> 20M
        q1b = {"cik": 2, "name": "BETA FUND LP", "period": "2026-03-31", "filed": "",
               "accession": "x", "total_value_usd": 10_000_000, "source_url": "http://example",
               "positions": {"010203040": pos("GAMMA LLC", "010203040", 10_000_000, 40000, "Decrease")}}
        q2b = {"cik": 2, "name": "BETA FUND LP", "period": "2026-06-30", "filed": "",
               "accession": "x", "total_value_usd": 20_000_000, "source_url": "http://example",
               "positions": {"010203040": pos("GAMMA LLC", "010203040", 20_000_000, 80000, "Increase")}}
        return [
            {"fund": FUND_A, "qoq": thirteenf.fund_qoq(q2a, q1a)},
            {"fund": FUND_B, "qoq": thirteenf.fund_qoq(q2b, q1b)},
        ]

    def test_convergence_detection_and_etf_exclusion(self):
        moves = thirteenf.top_moves(self._reports(), min_funds=2, include_etfs=False, limit=20)
        self.assertEqual(moves["period"], "2026-06-30")
        buyers = {r["issuer"]: r for r in moves["buys"]}
        # GAMMA: +55M new by Alpha AND +10M increase by Beta -> net +65M, convergence
        self.assertIn("GAMMA LLC", buyers)
        self.assertTrue(buyers["GAMMA LLC"]["convergence"])
        self.assertEqual(buyers["GAMMA LLC"]["funds_buying"], 2)
        self.assertEqual(buyers["GAMMA LLC"]["net_delta_usd"], 65_000_000)
        self.assertTrue(buyers["GAMMA LLC"]["is_new_anywhere"])
        # ETF excluded by default (Alpha +10M in the ETF)
        self.assertNotIn("VANGUARD S&P 500 ETF", buyers)
        # sells: BETA INC exited (-35M) and the ETF -10M (excluded)
        sellers = {r["issuer"]: r for r in moves["sells"]}
        self.assertIn("BETA INC", sellers)
        self.assertNotIn("VANGUARD S&P 500 ETF", sellers)

    def test_include_etfs_flag(self):
        moves = thirteenf.top_moves(self._reports(), min_funds=2, include_etfs=True, limit=20)
        buyers = {r["issuer"]: r for r in moves["buys"]}
        self.assertIn("VANGUARD S&P 500 ETF", buyers)
        self.assertTrue(buyers["VANGUARD S&P 500 ETF"]["etf"])

    def test_single_fund_move_not_convergence(self):
        moves = thirteenf.top_moves(self._reports(), min_funds=2, include_etfs=False, limit=20)
        buyers = {r["issuer"]: r for r in moves["buys"]}
        self.assertIn("ALPHA CORP", buyers)
        self.assertFalse(buyers["ALPHA CORP"]["convergence"])
        self.assertEqual(buyers["ALPHA CORP"]["funds_buying"], 1)

    def test_stale_fund_excluded_from_moves(self):
        # A fund whose latest 13F is from an older quarter must not pollute
        # the current-quarter convergence (e.g. a fund last filing in 2015).
        reports = self._reports()
        stale = tf.mk_quarter(b"""<informationTable><submitter><valueTotal>99999</valueTotal></submitter>
          <infoTable><nameOfIssuer>ZETA CORP</nameOfIssuer><titleOfClass>COM</titleOfClass>
          <cusip>099999999</cusip><value>99999</value><sshPrnamt>100</sshPrnamt>
          <issuanceType>Initial</issuanceType></infoTable></informationTable>""",
          "2024-12-31", name="STALE FUND LP", cik=99)
        reports.append({"fund": {"slug": "stale", "name": "STALE FUND LP",
                                 "manager": None, "cik": 99},
                        "qoq": thirteenf.fund_qoq(stale, None)})
        moves = thirteenf.top_moves(reports, min_funds=2, include_etfs=False, limit=20)
        issuers = {r["issuer"] for r in moves["buys"]} | {r["issuer"] for r in moves["sells"]}
        self.assertNotIn("ZETA CORP", issuers)  # 2024 quarter, excluded
        self.assertEqual(moves["period"], "2026-06-30")
        self.assertIn("GAMMA LLC", issuers)


class FundLookupTests(unittest.TestCase):
    def test_fund_by_slug_or_cik(self):
        f = thirteenf.fund_by_slug_or_cik("berkshire")
        self.assertEqual(f["cik"], 1067983)
        f = thirteenf.fund_by_slug_or_cik("1067983")
        self.assertEqual(f["slug"], "berkshire")
        self.assertIsNone(thirteenf.fund_by_slug_or_cik("nope-nope"))

    def test_basket_loads_17(self):
        funds = thirteenf.load_funds()
        self.assertGreaterEqual(len(funds), 15)
        slugs = {f["slug"] for f in funds}
        for expected in ("berkshire", "citadel", "tiger", "bridgewater", "pershing"):
            self.assertIn(expected, slugs)


if __name__ == "__main__":
    unittest.main()
