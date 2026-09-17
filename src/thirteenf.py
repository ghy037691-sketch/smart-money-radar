"""Form 13F engine: fetch, parse, quarter-over-quarter diff, cross-fund
convergence aggregation. Stdlib only. Values in filings are reported in
THOUSANDS of USD — normalized to whole USD here.
"""
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar  # noqa: E402
import names  # noqa: E402

FONDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "funds.json")

ETF_PREFIXES = (
    "VANGUARD", "ISSHARES", "ISHARES", "SPDR", "STATE STREET", "INVECO",
    "RELIANCE", "AMUNDI", "BLACKROCK", "JPMORGAN", "J P MORGAN", "XTRACKERS",
    "LYXOR", "HSBC", "UBS", "COMStage", "ETFS",
)


def load_funds():
    with open(FONDS_PATH, encoding="utf-8") as fh:
        return __import__("json").load(fh)["funds"]


def fund_by_slug_or_cik(query):
    q = str(query).strip().lower()
    for f in load_funds():
        if f["slug"] == q or f["name"].lower() == q or str(f["cik"]) == q:
            return f
    return None


def _tag_text(el, tag):
    for node in el.iter():
        if node.tag.split("}")[-1] == tag:
            return (node.text or "").strip()
    return ""


def parse_13f_xml(raw: bytes):
    """Parse a 13F information table. Returns (positions, total_value_usd).

    Two formats exist in the wild:
      - legacy (no XML namespace, has <submitter>/<valueTotal>): values in
        THOUSANDS of USD; rows carry putCall + issuanceType.
      - modern (xmlns=.../thirteenf/informationtable): values in WHOLE USD;
        same CUSIP may appear on multiple rows (one per sub-manager) and has
        no putCall/issuanceType. Sub-rows are aggregated per (cusip, putCall).
    """
    legacy = b"valueTotal" in raw
    scale = 1000 if legacy else 1
    root = ET.fromstring(raw)
    positions = {}
    for node in root.iter():
        if node.tag.split("}")[-1] != "infoTable":
            continue
        issuer = _tag_text(node, "nameOfIssuer")
        class_ = _tag_text(node, "titleOfClass")
        cusip = _tag_text(node, "cusip")
        put_call = _tag_text(node, "putCall") or ""
        try:
            value = int(float(_tag_text(node, "value") or 0)) * scale
        except ValueError:
            value = 0
        try:
            shares = float(_tag_text(node, "sshPrnamt") or 0)
        except ValueError:
            shares = 0
        key = (cusip or (issuer + "|" + class_), put_call)
        pos = positions.setdefault(key, {
            "issuer": issuer,
            "class": class_,
            "cusip": cusip,
            "value_usd": 0,
            "shares": 0.0,
            "put_call": put_call or None,
            "issuance": None,
        })
        pos["value_usd"] += value
        pos["shares"] += shares
        iss = _tag_text(node, "issuanceType")
        if iss and not pos["issuance"]:
            pos["issuance"] = iss
    out = {}
    for (cusip_key, put_call), p in positions.items():
        out[cusip_key if not put_call else f"{cusip_key}|{put_call}"] = p
    for p in out.values():
        p["shares"] = int(p["shares"]) if p["shares"] == int(p["shares"]) else p["shares"]
    total = sum(p["value_usd"] for p in out.values())
    return out, total


def _parses_as_info_table(raw: bytes) -> bool:
    """Strict check: well-formed XML containing infoTable/informationTable
    elements (namespace-agnostic)."""
    try:
        root = ET.fromstring(raw)
    except Exception:
        return False
    for node in root.iter():
        if node.tag.split("}")[-1] in ("infoTable", "informationTable"):
            return True
    return False


def _fetch_info_table(cik, accession, primary):
    """Fetch the 13F information-table XML.

    primaryDocument is NOT always the info table: some filers (e.g. Berkshire)
    use an HTML-in-XML display document, and info-table docs come in several
    shapes (legacy <informationTable>, default-namespace, and prefixed
    <ns1:infoTable>). Fallback: scan the filing index for .xml candidates in
    the accession directory and use the first that parses as an info table.
    """
    acc = accession.replace("-", "")
    primary_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{primary}"
    try:
        raw = edgar.get(primary_url, raw=True)
        if _parses_as_info_table(raw):
            return raw, primary_url
    except Exception:
        pass
    # NOTE: the index FILE is named with the dashed accession number inside the
    # undashed accession directory. (SEC returns 503, not 404, for wrong paths.)
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accession}-index.html"
    idx_html = edgar.get(idx_url, raw=True).decode("utf-8", "ignore")
    hrefs = re.findall(r'href="([^"]+\.xml)"', idx_html)
    # NOTE: Archives paths use UNPadded CIKs, so no digit-count filter. The
    # accession directory is the anchor; skip XSLT wrappers and schemas.
    cands = [h for h in hrefs
             if "/xsl" not in h.lower()
             and not h.lower().endswith(".xsd")
             and f"/{acc}/" in h]

    def score(h):
        hl = h.lower()
        if any(k in hl for k in ("infotable", "info_table", "form13f")):
            return 0
        if "primary" in hl:
            return 1
        return 2
    cands.sort(key=score)
    last_err = None
    for h in cands[:5]:
        url = ("https://www.sec.gov" + h) if h.startswith("/") else idx_url.rsplit("/", 1)[0] + "/" + h
        try:
            raw = edgar.get(url, raw=True)
        except Exception as exc:
            last_err = exc
            continue
        if _parses_as_info_table(raw):
            return raw, url
        last_err = ValueError(f"{h}: no infoTable markup")
    raise ValueError(f"13F information table not found in {accession}: {last_err or 'no candidates'}")


def fetch_fund_quarters(cik, n=2, progress=None):
    """Fetch the n most recent 13F-HR filings for a CIK and parse both."""
    sub = edgar.submissions(cik)
    rec = sub["filings"]["recent"]
    idxs = [i for i, f in enumerate(rec["form"]) if f == "13F-HR"][:n]
    out = []
    for i in idxs:
        acc = rec["accessionNumber"][i].replace("-", "")
        raw, url = _fetch_info_table(cik, rec["accessionNumber"][i], rec["primaryDocument"][i])
        positions, total = parse_13f_xml(raw)
        out.append({
            "cik": cik,
            "name": sub["name"],
            "period": rec["reportDate"][i],
            "filed": rec["filingDate"][i],
            "accession": rec["accessionNumber"][i],
            "total_value_usd": total,
            "positions": positions,
            "source_url": url,
        })
        if progress:
            progress(sub["name"], rec["reportDate"][i])
    return out


def _looks_like_etf(issuer, class_):
    i = (issuer or "").upper()
    c = (class_ or "").upper()
    if "INDEX" in c or "ETF" in c:
        return True
    if i.startswith(ETF_PREFIXES):
        return True
    if "ETF" in i or "INDEX" in i:
        return True
    return False


def fund_qoq(latest, prior=None):
    """Diff a fund's latest 13F against the prior quarter."""
    new, exited, increased, decreased = [], [], [], []
    prev = (prior or {}).get("positions", {})
    curr = (latest or {}).get("positions", {})
    for cusip, pos in curr.items():
        if cusip not in prev:
            new.append(pos)
            continue
        delta = pos["value_usd"] - prev[cusip]["value_usd"]
        if delta > 0:
            increased.append({**pos, "delta_usd": delta, "prev_value_usd": prev[cusip]["value_usd"]})
        elif delta < 0:
            decreased.append({**pos, "delta_usd": delta, "prev_value_usd": prev[cusip]["value_usd"]})
    for cusip, pos in prev.items():
        if cusip not in curr:
            exited.append({**pos, "delta_usd": -pos["value_usd"]})
    new.sort(key=lambda p: p["value_usd"], reverse=True)
    increased.sort(key=lambda p: p["delta_usd"], reverse=True)
    decreased.sort(key=lambda p: p["delta_usd"])
    return {
        "fund": latest["name"] if latest else None,
        "cik": latest["cik"] if latest else None,
        "period": latest["period"] if latest else None,
        "filed": latest["filed"] if latest else None,
        "total_value_usd": latest.get("total_value_usd") if latest else None,
        "prev_period": prior["period"] if prior else None,
        "prev_total_value_usd": prior.get("total_value_usd") if prior else None,
        "positions_count": len(curr),
        "prev_positions_count": len(prev),
        "new_positions": new,
        "exited_positions": exited,
        "increased": increased,
        "decreased": decreased,
        "source_url": latest.get("source_url") if latest else None,
    }


def top_moves(fund_reports, min_funds=2, include_etfs=False, limit=50):
    """Cross-fund convergence for the latest quarter.
    fund_reports: [{fund: {slug, name, manager}, qoq: fund_qoq result}]
    Returns buys, sells (ranked: convergence first, then net delta magnitude).
    """
    agg = {}
    for fr in fund_reports:
        if not fr.get("qoq") or not fr["qoq"].get("period"):
            continue
        q = fr["qoq"]
        fund_label = fr["fund"]["name"]
        # Rebuild per-fund delta map from qoq lists (keeps issuer metadata).
        movers = {}
        for p in q["new_positions"]:
            movers[p["cusip"] or (p["issuer"] + "|" + p["class"])] = (p["value_usd"], True, p)
        for p in q["increased"]:
            movers[p["cusip"] or (p["issuer"] + "|" + p["class"])] = (p["delta_usd"], False, p)
        for p in q["decreased"]:
            movers[p["cusip"] or (p["issuer"] + "|" + p["class"])] = (p["delta_usd"], False, p)
        for p in q["exited_positions"]:
            movers[p["cusip"] or (p["issuer"] + "|" + p["class"])] = (p["delta_usd"], False, p)

        for key, (delta, is_new, src) in movers.items():
            row = agg.setdefault(key, {
                "issuer": None, "class": None, "cusip": None, "ticker": None,
                "net_delta_usd": 0, "funds": [], "is_new_anywhere": False,
            })
            if not row["issuer"]:
                row["issuer"] = src.get("issuer")
                row["class"] = src.get("class")
                row["cusip"] = src.get("cusip")
            row["net_delta_usd"] += delta
            row["funds"].append({"fund": fund_label, "delta_usd": delta, "is_new": is_new})
            row["is_new_anywhere"] = row["is_new_anywhere"] or is_new

    # Merge rows that resolve to the same ticker: the same security filed
    # with a CUSIP by one fund and without by another must be one row.
    merged = {}
    for key, row in agg.items():
        t = names.ticker_for(row["issuer"] or "")
        row["ticker"] = t[0] if t else None
        mkey = ("T:" + row["ticker"].upper()) if row["ticker"] else ("K:" + key)
        m = merged.get(mkey)
        if m is None:
            row["funds"] = row["funds"][:]
            merged[mkey] = row
        else:
            m["net_delta_usd"] += row["net_delta_usd"]
            m["funds"].extend(row["funds"])
            m["is_new_anywhere"] = m["is_new_anywhere"] or row["is_new_anywhere"]
            for fld in ("issuer", "class", "cusip", "ticker"):
                if not m.get(fld) and row.get(fld):
                    m[fld] = row[fld]
    rows = list(merged.values())

    # A fund can file the same company under several CUSIPs (e.g. GOOG +
    # GOOGL, or common + options): net its moves into one entry per fund.
    for row in rows:
        by_fund = {}
        for f in row["funds"]:
            e = by_fund.setdefault(f["fund"], {"fund": f["fund"], "delta_usd": 0, "is_new": False})
            e["delta_usd"] += f["delta_usd"]
            e["is_new"] = e["is_new"] or f["is_new"]
        row["funds"] = list(by_fund.values())

    for row in rows:
        buys = [f for f in row["funds"] if f["delta_usd"] > 0]
        sells = [f for f in row["funds"] if f["delta_usd"] < 0]
        row["funds_buying"] = len(buys)
        row["funds_selling"] = len(sells)
        # convergence is side-specific: a buy row needs min_funds buying.
        row["convergence"] = None
        row["etf"] = _looks_like_etf(row["issuer"], row["class"])

    def finish(subset, side):
        for row in subset:
            row["convergence"] = (
                row["funds_buying"] if side == "buy" else row["funds_selling"]) >= min_funds
        out = [r for r in subset if include_etfs or not r["etf"]]
        out.sort(key=lambda r: (not r["convergence"],
                                -r["net_delta_usd"] if side == "buy" else r["net_delta_usd"]))
        return out[:limit]

    buys = finish([r for r in rows if r["net_delta_usd"] > 0], "buy")
    sells = finish([r for r in rows if r["net_delta_usd"] < 0], "sell")
    return {"period": _latest_period(fund_reports), "buys": buys, "sells": sells}


def _latest_period(fund_reports):
    periods = [fr["qoq"]["period"] for fr in fund_reports
               if fr.get("qoq") and fr["qoq"].get("period")]
    return max(periods) if periods else None


def fund_report(fund, min_positions_value=0, top_n=15):
    """Fetch latest two quarters for one fund and return the QoQ report."""
    quarters = fetch_fund_quarters(fund["cik"], n=2)
    if not quarters:
        return {"error": f"No 13F-HR filings found for {fund['name']} ({fund['cik']})"}
    qoq = fund_qoq(quarters[0], quarters[1] if len(quarters) > 1 else None)
    qoq["fund_slug"] = fund["slug"]
    qoq["manager"] = fund.get("manager")
    top = sorted(quarters[0]["positions"].values(), key=lambda p: p["value_usd"], reverse=True)[:top_n]
    for p in top:
        t = names.ticker_for(p["issuer"])
        p["ticker"] = t[0] if t else None
        if qoq.get("total_value_usd"):
            p["pct_of_portfolio"] = round(100.0 * p["value_usd"] / qoq["total_value_usd"], 2)
    qoq["top_positions"] = top
    qoq["all_positions"] = [
        {
            "issuer": p["issuer"], "class": p["class"], "cusip": p["cusip"],
            "value_usd": p["value_usd"], "shares": p["shares"],
            "put_call": p["put_call"], "issuance": p["issuance"],
            "ticker": None,
        }
        for p in quarters[0]["positions"].values()
    ]
    return qoq


def stock_holdings(symbol, fund_reports=None):
    """Which tracked funds hold a security (by ticker, CUSIP, or issuer name).

    Tickers are resolved to the official SEC company title first, so
    'NVDA' matches NVIDIA CORP and not the '2X SHORT NVDA' ETFs.
    """
    if fund_reports is None:
        fund_reports = build_fund_reports()
    q = str(symbol).strip().lower()
    if not q:
        return []
    # ticker -> official title (normalized key) for strict issuer matching
    issuer_key = None
    if q.isalnum() and len(q) <= 6:
        t = names.ticker_to_title(q)
        if t:
            issuer_key = t[0]
    out = []
    for fr in fund_reports:
        qu = fr["qoq"]
        if not qu or not qu.get("period"):
            continue
        latest = None
        for p in qu.get("all_positions") or []:
            if _security_matches(p, q, issuer_key):
                latest = p
                break
        if latest is None:
            continue
        ticker = latest.get("ticker")
        if not ticker:
            t = names.ticker_for(latest.get("issuer") or "")
            ticker = t[0] if t else None
        value = latest.get("value_usd") or 0
        total = qu.get("total_value_usd")
        out.append({
            "fund": fr["fund"]["name"],
            "slug": fr["fund"]["slug"],
            "manager": fr["fund"].get("manager"),
            "ticker": ticker,
            "issuer": latest.get("issuer"),
            "cusip": latest.get("cusip"),
            "class": latest.get("class"),
            "put_call": latest.get("put_call"),
            "value_usd": value,
            "pct_of_portfolio": round(100.0 * value / total, 2) if total else None,
            "period": qu["period"],
            "movement": _movement_label(fr, latest),
        })
    out.sort(key=lambda r: r["value_usd"] or 0, reverse=True)
    return out


def _security_matches(p, q, issuer_key=None):
    """Match a position against a query.
    - CUSIP: exact (alphanumeric, 6-12 chars).
    - Ticker: exact, when the position has one.
    - Issuer: strict normalized comparison when the query was resolved to an
      official title (issuer_key), otherwise substring (min 4 chars).
    Never matches on class/other free-text — that's how 'NVDA' used to hit
      '2X SHORT NVDA' ETFs.
    """
    if q.isalnum() and 6 <= len(q) <= 12 and (p.get("cusip") or "").lower() == q:
        return True
    if p.get("ticker") and str(p["ticker"]).lower() == q:
        return True
    issuer = p.get("issuer") or ""
    if issuer_key:
        ni = names.normalize(issuer)
        return bool(ni) and (ni == issuer_key or issuer_key in ni or ni in issuer_key)
    if len(q) >= 4 and q in issuer.lower():
        return True
    return False


def _movement_label(fr, pos):
    key = pos.get("cusip") or (pos.get("issuer") + "|" + pos.get("class"))
    qu = fr["qoq"]
    for p in qu["new_positions"]:
        if (p["cusip"] or (p["issuer"] + "|" + p["class"])) == key:
            return "NEW this quarter"
    for p in qu["increased"]:
        if (p["cusip"] or (p["issuer"] + "|" + p["class"])) == key:
            return f"INCREASED +{p['delta_usd']:,}"
    for p in qu["decreased"]:
        if (p["cusip"] or (p["issuer"] + "|" + p["class"])) == key:
            return f"DECREASED {p['delta_usd']:,}"
    return "held"


def build_fund_reports(funds=None, progress=None):
    """Build QoQ reports for the whole basket (the expensive step, ~1-2 min
    cold with SEC throttling). This is what the web cache and the actor hold."""
    funds = funds or load_funds()
    reports = []
    for fund in funds:
        try:
            reports.append({"fund": fund, "qoq": fund_report(fund)})
        except Exception as exc:  # one fund failing must not kill the basket
            reports.append({"fund": fund, "qoq": None, "error": str(exc)})
        if progress:
            progress(fund["name"], len(reports))
    return reports


def insider_buys(days_back=30, min_value_usd=100_000, limit=100, parse_cap=80):
    """Recent open-market Form 4 purchases above a value threshold, with
    cluster detection (>=2 distinct insiders buying the same company in the
    window)."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)
    hits, total = edgar.ftsearch(forms=["4"], start=start.isoformat(),
                                 end=end.isoformat(), limit=min(limit * 3, 300))
    rows = []
    parsed = 0
    for h in hits:
        if parsed >= parse_cap or len(rows) >= limit:
            break
        parsed += 1
        t = edgar.get(h["filing_index"], raw=True) if h["filing_index"] else b""
        if b"ownershipDocument" not in t:
            continue
        # find the wk-form4 primary xml in the index
        hrefs = re.findall(rb'href="([^"]+\.xml)"', t)
        doc = None
        for href in hrefs:
            s = href.decode("latin-1", "ignore")
            if "/xsl" in s or s.lower().endswith(".xsd"):
                continue
            if re.search(r"/\d{10}/(?!xsl)", s, re.I):
                doc = s
                break
        if not doc:
            continue
        if doc.startswith("/"):
            doc = "https://www.sec.gov" + doc
        elif not doc.startswith("http"):
            doc = h["filing_index"].rsplit("/", 1)[0] + "/" + doc
        try:
            raw4 = edgar.get(doc, raw=True)
            root = ET.fromstring(raw4)
        except Exception:
            continue
        insider = None
        roles = []
        txns = []
        for node in root.iter():
            tag = node.tag.split("}")[-1]
            if tag == "rptOwnerName" and insider is None:
                insider = (node.text or "").strip()
            elif tag in ("isDirector", "isOfficer", "isTenPercentOwner"):
                if (node.text or "").strip() == "1":
                    roles.append({"isDirector": "Director", "isOfficer": "Officer",
                                  "isTenPercentOwner": "10% owner"}[tag])
            elif tag == "nonDerivativeTransaction":
                def g(t):
                    for el in node.iter():
                        if el.tag.split("}")[-1] == t:
                            return (el.text or "").strip()
                    return ""
                code = g("transactionCode")
                if code != "P":  # only open-market purchases are buy signals
                    continue
                try:
                    shares = float(g("transactionShares") or 0)
                    price = float(g("transactionPricePerShare") or 0)
                except ValueError:
                    continue
                value = shares * price
                if value < min_value_usd:
                    continue
                rows.append({
                    "company": h["company"], "ticker": h.get("ticker"), "cik": h.get("cik"),
                    "insider": insider, "roles": roles,
                    "date": g("transactionDate"),
                    "shares": int(shares) if shares == int(shares) else shares,
                    "price_per_share": price or None,
                    "value_usd": round(value),
                    "filed": h.get("filed"),
                    "source_url": h.get("filing_index"),
                })
    # cluster detection
    by_company = {}
    for r in rows:
        by_company.setdefault(r["company"], set()).add(r["insider"] or "?")
    clusters = {c for c, s in by_company.items() if len(s) >= 2}
    for r in rows:
        r["cluster"] = r["company"] in clusters
        r["signal"] = "CLUSTER INSIDER BUY" if r["cluster"] else "insider buy"
    rows.sort(key=lambda r: (not r["cluster"], -r["value_usd"]))
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "filings_scanned": parsed,
        "matched_total": total,
        "min_value_usd": min_value_usd,
        "returned": len(rows[:limit]),
        "clusters_found": len(clusters),
        "insider_buys": rows[:limit],
    }
