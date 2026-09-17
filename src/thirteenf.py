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
    positions: {cusip: {issuer, class, cusip, value_usd, shares, put_call, issuance}}
    """
    root = ET.fromstring(raw)
    positions = {}
    total = None
    for node in root.iter():
        t = node.tag.split("}")[-1]
        if t == "valueTotal" and total is None:
            try:
                total = int(float((node.text or "0").strip())) * 1000
            except ValueError:
                total = None
        elif t == "infoTable":
            issuer = _tag_text(node, "nameOfIssuer")
            class_ = _tag_text(node, "titleOfClass")
            cusip = _tag_text(node, "cusip")
            try:
                value = int(float(_tag_text(node, "value") or 0)) * 1000
            except ValueError:
                value = 0
            try:
                shares = float(_tag_text(node, "sshPrnamt") or 0)
            except ValueError:
                shares = None
            positions[cusip or (issuer + "|" + class_)] = {
                "issuer": issuer,
                "class": class_,
                "cusip": cusip,
                "value_usd": value,
                "shares": int(shares) if shares is not None else None,
                "put_call": _tag_text(node, "putCall") or None,
                "issuance": _tag_text(node, "issuanceType") or None,
            }
    return positions, total


def _fetch_info_table(cik, accession, primary):
    """Fetch the 13F information-table XML.

    primaryDocument is NOT always the info table: some filers (e.g. Berkshire)
    use an HTML-in-XML display document. Fallback: scan the filing index for
    .xml candidates in the accession directory and use the first that contains
    the infoTable markup.
    """
    acc = accession.replace("-", "")
    primary_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{primary}"
    try:
        raw = edgar.get(primary_url, raw=True)
        if b"<infoTable" in raw or b"<informationTable" in raw:
            return raw, primary_url
    except Exception:
        pass
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{acc}-index.html"
    idx_html = edgar.get(idx_url, raw=True).decode("utf-8", "ignore")
    hrefs = re.findall(r'href="([^"]+\.xml)"', idx_html)
    cands = [h for h in hrefs
             if "/xsl" not in h and not h.lower().endswith(".xsd")
             and re.search(r"/\d{10}/", h, re.I)]

    def score(h):
        hl = h.lower()
        return 0 if any(k in hl for k in ("infotable", "info_table", "form13f", "primary")) else 1
    cands.sort(key=score)
    last_err = None
    for h in cands[:6]:
        url = ("https://www.sec.gov" + h) if h.startswith("/") else idx_url.rsplit("/", 1)[0] + "/" + h
        try:
            raw = edgar.get(url, raw=True)
        except Exception as exc:
            last_err = exc
            continue
        if b"<infoTable" in raw or b"<informationTable" in raw:
            return raw, url
    raise ValueError(f"13F information table not found in {accession}: {last_err or 'no candidates parsed'}")


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
    Returns buys, sells (ranked by net delta across the basket).
    """
    agg = {}
    for fr in fund_reports:
        if not fr.get("qoq") or not fr["qoq"].get("period"):
            continue
        q = fr["qoq"]
        fund_label = fr["fund"]["name"]
        # Rebuild per-fund delta map from qoq lists (keeps issuer metadata).
        delta_by_cusip = {}
        for p in q["new_positions"]:
            delta_by_cusip[p["cusip"] or (p["issuer"] + "|" + p["class"])] = (
                p["value_usd"], True)
        for p in q["increased"]:
            key = p["cusip"] or (p["issuer"] + "|" + p["class"])
            delta_by_cusip[key] = (p["delta_usd"], False)
        for p in q["decreased"]:
            key = p["cusip"] or (p["issuer"] + "|" + p["class"])
            delta_by_cusip[key] = (p["delta_usd"], False)
        for p in q["exited_positions"]:
            key = p["cusip"] or (p["issuer"] + "|" + p["class"])
            delta_by_cusip[key] = (p["delta_usd"], False)

        for key, (delta, is_new) in delta_by_cusip.items():
            row = agg.setdefault(key, {
                "issuer": None, "class": None, "cusip": None, "ticker": None,
                "net_delta_usd": 0, "funds": [], "is_new_anywhere": False,
            })
            # keep the richest metadata we've seen for this security
            src = None
            for lst in (q["new_positions"], q["increased"], q["decreased"], q["exited_positions"]):
                for p in lst:
                    k = p["cusip"] or (p["issuer"] + "|" + p["class"])
                    if k == key:
                        src = p
                        break
                if src:
                    break
            if src:
                row["issuer"] = src.get("issuer") or row["issuer"]
                row["class"] = src.get("class") or row["class"]
                row["cusip"] = src.get("cusip") or row["cusip"]
            row["net_delta_usd"] += delta
            row["funds"].append({
                "fund": fund_label, "delta_usd": delta, "is_new": is_new,
            })
            row["is_new_anywhere"] = row["is_new_anywhere"] or is_new

    def finish(rows):
        for row in rows:
            buys = [f for f in row["funds"] if f["delta_usd"] > 0]
            sells = [f for f in row["funds"] if f["delta_usd"] < 0]
            row["funds_buying"] = len(buys)
            row["funds_selling"] = len(sells)
            row["convergence"] = len(buys) >= min_funds or len(sells) >= min_funds
            row["etf"] = _looks_like_etf(row["issuer"], row["class"])
            if not row["ticker"]:
                t = names.ticker_for(row["issuer"] or "")
                if t:
                    row["ticker"] = t[0]
        if not include_etfs:
            rows = [r for r in rows if not r["etf"]]
        return rows[:limit]

    buys = finish([r for r in agg.values() if r["net_delta_usd"] > 0])
    buys.sort(key=lambda r: r["net_delta_usd"], reverse=True)
    sells = finish([r for r in agg.values() if r["net_delta_usd"] < 0])
    sells.sort(key=lambda r: r["net_delta_usd"])
    # convergence first, then by magnitude
    buys.sort(key=lambda r: (not r["convergence"], -r["net_delta_usd"]))
    sells.sort(key=lambda r: (not r["convergence"], r["net_delta_usd"]))
    return {"period": _latest_period(fund_reports), "buys": buys[:limit], "sells": sells[:limit]}


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
    """Which tracked funds hold a security (by ticker, CUSIP, or issuer name)."""
    if fund_reports is None:
        fund_reports = build_fund_reports()
    q = str(symbol).strip().lower()
    out = []
    for fr in fund_reports:
        qu = fr["qoq"]
        if not qu or not qu.get("period"):
            continue
        latest = None
        for p in qu.get("all_positions") or []:
            if _security_matches(p, q):
                latest = p
                break
        if latest is None:
            continue
        ticker = latest.get("ticker")
        if not ticker:
            t = names.ticker_for(latest.get("issuer") or "")
            ticker = t[0] if t else None
        out.append({
            "fund": fr["fund"]["name"],
            "slug": fr["fund"]["slug"],
            "manager": fr["fund"].get("manager"),
            "ticker": ticker,
            "issuer": latest.get("issuer"),
            "value_usd": latest.get("value_usd"),
            "pct_of_portfolio": latest.get("pct_of_portfolio"),
            "period": qu["period"],
            "movement": _movement_label(fr, latest),
        })
    out.sort(key=lambda r: r["value_usd"] or 0, reverse=True)
    return out


def _security_matches(p, q):
    for field in ("ticker", "cusip", "issuer", "class"):
        v = (p.get(field) or "").lower()
        if v and (v == q or q in v):
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
