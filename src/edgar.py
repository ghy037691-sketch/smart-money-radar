"""SEC EDGAR client — free, no API key, official JSON/XML endpoints.
Proven pattern: custom User-Agent (SEC fair access), throttled starts, gzip,
bounded retries. Mirrors edgar-signals' production client.
"""
import gzip
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = __import__("os").environ.get(
    "SEC_EDGAR_USER_AGENT",
    "smart-money-radar/1.0 (contact: contact@edgar-signals.onrender.com)",
)
BASE = "https://data.sec.gov"
_last = [0.0]
_lock = threading.Lock()


def _throttle():
    """Rate-limit request starts to ~8/s; lock held only for the check/update."""
    with _lock:
        gap = time.time() - _last[0]
        if gap < 0.15:
            time.sleep(0.15 - gap)
        _last[0] = time.time()


def get(url, raw=False, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
        "Accept": "application/json, application/xml, text/html;q=0.8, */*;q=0.5",
        "Host": url.split("/")[2],
    })
    last_err = None
    for attempt in range(5):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            return data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504):
                # SEC blocks bad-behavior clients hard: back off exponentially.
                time.sleep(2.0 * (2 ** attempt))
                continue
            raise
    raise last_err


def submissions(cik):
    return get(f"{BASE}/submissions/CIK{int(cik):010d}.json")


def ftsearch(q=None, forms=None, start=None, end=None, limit=100):
    """Full-text search via efts.sec.gov. Returns (hits, total)."""
    out = []
    total = None
    offset = 0
    page = 100
    while len(out) < limit:
        params = {}
        if q:
            params["q"] = q
        if forms:
            params["forms"] = ",".join(forms) if isinstance(forms, (list, tuple)) else forms
        if start or end:
            params["dateRange"] = "custom"
            if start:
                params["startdt"] = start
            if end:
                params["enddt"] = end
        params["start"] = offset
        d = get("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params))
        hits = d.get("hits", {}).get("hits", [])
        if total is None:
            total = d.get("hits", {}).get("total", {}).get("value")
        if not hits:
            break
        for h in hits:
            src = h.get("_source", {})
            disp = (src.get("display_names") or [""])[0]
            company = re.split(r"\s{2,}", disp)[0]
            company = re.sub(r"\s*\([A-Z0-9.,\- ]{1,16}$", "", company).strip()
            ticker_m = re.search(r"\(([A-Z0-9.\-]{1,10})\)\s*\(CIK", disp)
            ciks = src.get("ciks") or []
            cik = int(ciks[0]) if ciks else None
            adsh = src.get("adsh") or ""
            form_val = src.get("form")
            if isinstance(form_val, list):
                form_val = ",".join(form_val)
            out.append({
                "company": company or disp,
                "ticker": ticker_m.group(1) if ticker_m else None,
                "cik": cik,
                "form": form_val or "",
                "filed": src.get("file_date") or src.get("fileDate"),
                "accession": adsh,
                "filing_index": (
                    f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{adsh}-index.html"
                    if cik and adsh else ""
                ),
            })
            if len(out) >= limit:
                break
        if len(hits) < page:
            break
        offset += page
    return out, total


def resolve_fund(name):
    """Resolve a fund name to a CIK via recent 13F-HR full-text search."""
    hits, _ = ftsearch(q=f'"{name}"', forms=["13F-HR"],
                       start=(datetime.now(timezone.utc).date() - timedelta(days=180)).isoformat(),
                       limit=10)
    for h in hits:
        if h["cik"]:
            sub = submissions(h["cik"])
            if name.lower()[:8] in sub["name"].lower():
                return sub["name"], h["cik"]
    for h in hits:
        if h["cik"]:
            return h["company"], h["cik"]
    return None, None


def ticker_universe():
    """SEC's official CIK->ticker file (updated weekly, no key)."""
    data = get("https://www.sec.gov/files/company_tickers.json")
    out = []
    for v in data.values():
        out.append((int(v["cik_str"]), v["ticker"], v["title"]))
    return out
