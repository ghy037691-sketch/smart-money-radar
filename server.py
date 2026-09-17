#!/usr/bin/env python3
"""Smart Money Radar — production-shaped, zero-dependency HTTP server.

The public Render deployment is the live product surface: cached cross-fund
smart-money moves (recomputed at startup and on demand), per-fund flows,
stock holdings lookup, and insider-buy signals. The Apify Actor (main.py) is
the billable per-run surface; both share the same engine in src/.
"""
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
import edgar  # noqa: E402
import thirteenf  # noqa: E402

VERSION = "1.0.0"
ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
CACHE_PATH = os.path.join(ROOT, "cache", "smart-money-cache.json")
MAX_BODY = 64 * 1024
CACHE_TTL_SECONDS = 24 * 3600
INSIDER_TTL_SECONDS = 6 * 3600

INDEX_HTML = open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8").read()

_state = {
    "lock": threading.Lock(),
    "status": "warming",          # warming | ready | error
    "started_at": time.time(),
    "generated_at": None,
    "funds": None,
    "qoq": {},                    # slug -> fund qoq report (incl. top+all positions)
    "moves": None,                # {"period", "buys", "sells"}
    "errors": {},
    "insider": None,              # {"fetched_at", "data"}
    "insider_lock": threading.Lock(),
}
_rate_lock = threading.Lock()
_rate_windows = defaultdict(deque)


def _client_allowed(client, limit, window=60):
    now = time.monotonic()
    with _rate_lock:
        w = _rate_windows[client]
        while w and now - w[0] > window:
            w.popleft()
        if len(w) >= limit:
            return False
        w.append(now)
        return True


def _publish(reports, errors):
    """Publish whatever is ready so far (progressive availability).
    Heavy work (top_moves) happens OUTSIDE the state lock so /api/health
    never blocks — a blocked health endpoint makes Render restart the box.
    """
    funds = []
    qoq_map = {}
    for r in reports:
        fund, qoq = r["fund"], r.get("qoq")
        funds.append({
            "slug": fund["slug"], "name": fund["name"], "manager": fund.get("manager"),
            "cik": fund["cik"],
            "period": qoq["period"] if qoq else None,
            "total_value_usd": qoq.get("total_value_usd") if qoq else None,
            "positions_count": qoq.get("positions_count") if qoq else None,
            "new_count": len(qoq["new_positions"]) if qoq else 0,
            "exited_count": len(qoq["exited_positions"]) if qoq else 0,
            "error": r.get("error"),
        })
        if qoq:
            qoq_map[fund["slug"]] = qoq
    moves = thirteenf.top_moves(reports)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _state["lock"]:
        _state["funds"] = funds
        _state["qoq"] = qoq_map
        _state["errors"] = dict(errors)
        _state["moves"] = moves
        _state["generated_at"] = generated_at
        if any(qoq_map.values()):
            _state["status"] = "ready"


def _build():
    """Background build of the whole basket, publishing incrementally:
    the site goes ready as soon as the first fund parses, then fills in.
    Disk cache is written once at the end (per-fund writes are too heavy
    for the free tier's disk and I/O budget).
    """
    reports = []
    errors = {}
    for fund in thirteenf.load_funds():
        try:
            qoq = thirteenf.fund_report(fund)
            if "error" in qoq:
                raise ValueError(qoq["error"])
            reports.append({"fund": fund, "qoq": qoq})
            print(f"smart-money-radar: fund ready {fund['slug']} ({len(reports)})", flush=True)
        except Exception as exc:
            errors[fund["slug"]] = str(exc)
            reports.append({"fund": fund, "qoq": None, "error": str(exc)})
            print(f"smart-money-radar: fund failed {fund['slug']}: {exc}", file=sys.stderr, flush=True)
        _publish(reports, errors)
    _save_disk_cache()
    print(f"smart-money-radar: basket build complete at {datetime.now(timezone.utc).isoformat()}", flush=True)


def _save_disk_cache():
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with _state["lock"]:
            payload = {
                "generated_at": _state["generated_at"],
                "funds": list(_state["funds"] or []),
                "qoq": dict(_state["qoq"]),
                "moves": _state["moves"],
            }
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
    except Exception as exc:
        print(f"smart-money-radar: disk cache write failed: {exc}", file=sys.stderr)


def _load_disk_cache():
    """Load a fresh disk cache (survives restarts on the same FS)."""
    try:
        if not os.path.isfile(CACHE_PATH):
            return False
        with open(CACHE_PATH, encoding="utf-8") as fh:
            payload = json.load(fh)
        gen = payload.get("generated_at")
        if not gen:
            return False
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(gen)).total_seconds()
        if age > CACHE_TTL_SECONDS:
            return False
        with _state["lock"]:
            _state["funds"] = payload.get("funds")
            _state["qoq"] = payload.get("qoq") or {}
            _state["moves"] = payload.get("moves")
            _state["generated_at"] = gen
            if _state["moves"]:
                _state["status"] = "ready"
        print(f"smart-money-radar: loaded disk cache (age {int(age)}s)", flush=True)
        return True
    except Exception:
        return False


def _get_insider(days=30, min_value=100_000, limit=100):
    """Fetch (or reuse) the insider scan; cached by (days, min_value), sliced to limit."""
    key = (days, min_value)
    with _state["insider_lock"]:
        cached = _state["insider"]
        if cached and cached["key"] == key and time.time() - cached["fetched_at"] < INSIDER_TTL_SECONDS:
            return {**cached["data"], "insider_buys": cached["data"]["insider_buys"][:limit]}
        data = thirteenf.insider_buys(days_back=days, min_value_usd=min_value, limit=max(limit, 100))
        _state["insider"] = {"fetched_at": time.time(), "key": key, "data": data}
        return {**data, "insider_buys": data["insider_buys"][:limit]}


def _insider_warm():
    time.sleep(15)  # let the basket build start first
    try:
        _get_insider()
        print("smart-money-radar: insider scan warm", flush=True)
    except Exception as exc:
        print(f"smart-money-radar: insider warm failed: {exc}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartMoneyRadar/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _ip(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0] or "?").split(",")[0].strip()

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store" if ctype.startswith("application/json") else "max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, body, code=200):
        self._send(code, body.encode("utf-8"), "text/html; charset=utf-8")

    # ---------------- GET ----------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        if not _client_allowed(self._ip(), 300, 60):
            self._json({"error": "rate limit exceeded"}, 429)
            return

        if path in ("/", "/index.html"):
            self._html(INDEX_HTML)
            return
        if path == "/favicon.ico":
            self._send(200, open(os.path.join(WEB_DIR, "logo.svg"), "rb").read(), "image/svg+xml")
            return
        if path.startswith("/static/"):
            name = os.path.basename(path)
            f = os.path.join(WEB_DIR, name)
            if os.path.isfile(f) and not name.startswith("."):
                ctype = {
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".json": "application/json",
                    ".txt": "text/plain; charset=utf-8",
                    ".html": "text/html; charset=utf-8",
                }.get(os.path.splitext(name)[1], "application/octet-stream")
                self._send(200, open(f, "rb").read(), ctype)
                return

        if path == "/api/health":
            # Lock-free on purpose: the platform health probe must answer
            # instantly even while the build thread is publishing.
            self._json({"ok": True, "service": "smart-money-radar", "version": VERSION,
                        "cache_status": _state["status"],
                        "generated_at": _state["generated_at"],
                        "time": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            return
        if path == "/api/version":
            self._json({"service": "smart-money-radar", "version": VERSION})
            return
        if path == "/api/status":
            with _state["lock"]:
                body = {
                    "status": _state["status"],
                    "generated_at": _state["generated_at"],
                    "funds_ready": sum(1 for f in (_state["funds"] or []) if f.get("period")),
                    "funds_total": len(_state["funds"] or []),
                    "errors": dict(_state["errors"]),
                }
            self._json(body)
            return
        if path == "/api/funds":
            with _state["lock"]:
                if _state["status"] != "ready" or not _state["funds"]:
                    self._json({"status": "warming", "message": "Fetching 13F filings from SEC EDGAR — retry shortly."}, 202)
                    return
                self._json({"funds": _state["funds"], "generated_at": _state["generated_at"]})
            return
        if path == "/api/moves":
            include_etfs = (q.get("include_etfs") or [""])[0] in ("1", "true")
            limit = min(int((q.get("limit") or ["50"])[0] or 50), 200)
            with _state["lock"]:
                if _state["status"] != "ready" or not _state["moves"]:
                    self._json({"status": "warming", "message": "Fetching 13F filings from SEC EDGAR — retry shortly."}, 202)
                    return
                moves = _state["moves"]
                if include_etfs:
                    all_funds = [r for r in ([{"fund": {"name": f["name"]}, "qoq": _state["qoq"][f["slug"]]}
                                              for f in _state["funds"] if _state["qoq"].get(f["slug"])])]
                    moves = thirteenf.top_moves(all_funds, include_etfs=True, limit=limit)
            self._json({"quarter": moves["period"], "buys": moves["buys"][:limit],
                        "sells": moves["sells"][:limit], "generated_at": _state["generated_at"]})
            return
        if path == "/api/fund":
            slug = (q.get("slug") or [""])[0]
            with _state["lock"]:
                report = _state["qoq"].get(slug)
                if _state["status"] != "ready" or not report:
                    self._json({"status": "warming", "message": "Data not ready yet — retry shortly."}, 202)
                    return
            public = {k: v for k, v in report.items() if k != "all_positions"}
            self._json(public)
            return
        if path == "/api/stock":
            sym = (q.get("q") or [""])[0].strip()
            if not sym:
                self._json({"error": "provide ?q= (ticker, CUSIP, or name)"}, 400)
                return
            if not _client_allowed(self._ip() + "|stock", 10, 60):
                self._json({"error": "rate limit exceeded"}, 429)
                return
            with _state["lock"]:
                if _state["status"] != "ready":
                    self._json({"status": "warming", "message": "Data not ready yet — retry shortly."}, 202)
                    return
                reports = [{"fund": {"slug": f["slug"], "name": f["name"], "manager": f.get("manager")},
                            "qoq": _state["qoq"][f["slug"]]}
                           for f in _state["funds"] if _state["qoq"].get(f["slug"])]
            holdings = thirteenf.stock_holdings(sym, fund_reports=reports)
            self._json({"symbol": sym, "quarter": thirteenf._latest_period(reports),
                        "funds_holding": len(holdings), "holdings": holdings})
            return
        if path == "/api/insider":
            if not _client_allowed(self._ip() + "|insider", 6, 60):
                self._json({"error": "rate limit exceeded"}, 429)
                return
            days = min(int((q.get("days") or ["30"])[0] or 30), 365)
            try:
                data = _get_insider(days=days)
            except Exception as exc:
                self._json({"error": f"upstream fetch failed: {type(exc).__name__}. Retry later."}, 502)
                return
            self._json(data)
            return
        if path == "/api/refresh":
            if not _client_allowed(self._ip() + "|refresh", 1, 300):
                self._json({"error": "rate limit exceeded"}, 429)
                return
            with _state["lock"]:
                if _state["status"] == "warming":
                    self._json({"status": "warming"})
                    return
                _state["status"] = "warming"
            threading.Thread(target=_build, daemon=True).start()
            self._json({"status": "warming", "message": "Rebuilding basket from SEC EDGAR."})
            return

        if path == "/robots.txt":
            body = "User-agent: *\nAllow: /\n"
            self._send(200, body.encode("utf-8"), "text/plain; charset=utf-8")
            return
        self._json({"error": "not found"}, 404)


def main():
    port = int(os.environ.get("PORT", "8000"))
    fresh = _load_disk_cache()
    if not fresh or _state["status"] != "ready":
        threading.Thread(target=_build, daemon=True).start()
    threading.Thread(target=_insider_warm, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"smart-money-radar v{VERSION} listening on 0.0.0.0:{port} (cache={'warm' if fresh else 'cold'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
