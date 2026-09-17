"""Issuer name -> ticker mapping using the SEC's official company_tickers.json.

13F positions carry CUSIPs, not tickers. CUSIP->ticker APIs (OpenFIGI) require
keys; the keyless official path is matching the issuer name against the SEC's
~12k company titles. Cached in memory with a 7-day TTL.
"""
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar  # noqa: E402

_cache = {"at": 0.0, "index": None, "by_ticker": None}
_lock = threading.Lock()
TTL = 7 * 24 * 3600

STOP = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LLC", "LTD",
    "LP", "L P", "PLC", "SA", "S A", "AG", "KG", "GMBH", "NV", "B A", "BVA",
    "BV", "SARL", "SRL", "S R L", "SP Z O O", "SCA", "SE", "AB", "AS", "SAF",
    "GROUP", "HOLDINGS", "HOLDINGS CORP", "DE", "OF", "THE", "AND", "AMP",
    "AMERICAN", "UNITED", "U S", "NEW", "INTERNATIONAL", "GLOBAL",
}


def _norm(name: str) -> str:
    s = (name or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    tokens = [t for t in s.split() if t not in STOP]
    return " ".join(tokens)


def _load():
    with _lock:
        if _cache["index"] is not None and time.time() - _cache["at"] < TTL:
            return _cache["index"]
        universe = edgar.ticker_universe()
        index = {}
        by_ticker = {}
        for _cik, ticker, title in universe:
            key = _norm(title)
            if len(key) >= 4 and key not in index:
                index[key] = (ticker, title)
                by_ticker.setdefault(ticker.upper(), key)
        _cache.update(index=index, by_ticker=by_ticker, at=time.time())
        return index


def normalize(name: str) -> str:
    return _norm(name)


def ticker_to_title(ticker: str):
    """Reverse lookup: ticker -> (normalized SEC title key, raw title)."""
    try:
        _load()
    except Exception:
        return None
    t = (ticker or "").strip().upper()
    if not t:
        return None
    with _lock:
        by_ticker = _cache["by_ticker"]
        key = by_ticker.get(t) if by_ticker else None
        if not key:
            return None
        pair = _cache["index"].get(key)
    return (key, pair[1]) if pair else None


def ticker_for(issuer: str):
    """Return (ticker, sec_title) or None. Exact normalized match first,
    then bidirectional containment (>= 8 normalized chars)."""
    try:
        index = _load()
    except Exception:
        return None
    key = _norm(issuer)
    if not key or len(key) < 4:
        return None
    if key in index:
        return index[key]
    best = None
    for k, v in index.items():
        if len(k) < 8:
            continue
        if (k in key) or (key in k):
            if best is None or abs(len(k) - len(key)) < abs(len(best[0]) - len(key)):
                best = (k, v)
    if best:
        return best[1]
    return None
