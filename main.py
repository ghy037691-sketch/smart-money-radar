"""Smart Money Radar — Apify Actor entry point (Python standard library only).

Actions:
  top_moves      Cross-fund convergence: what the tracked smart-money funds
                 are all buying/selling this quarter (default)
  fund_flows     One fund's latest 13F: top positions + QoQ new/increased/
                 decreased/exited
  stock_holdings Which tracked funds hold a security (ticker / CUSIP / name)
  insider_buys   Recent large open-market Form 4 purchases + cluster signals

One run performs one action. Customer-visible records go to the default
dataset (billable); the complete result goes to the default key-value store
under OUTPUT. Errors produce a sanitized, zero-billable result.
"""
from datetime import datetime, timezone
import glob
import json
import math
import os
import sys
import time
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
import edgar  # noqa: E402
import thirteenf  # noqa: E402

ACTOR_VERSION = "1.0"
CANONICAL_ACTIONS = ("top_moves", "fund_flows", "stock_holdings", "insider_buys")
ACTION_ALIASES = {
    "top_moves": "top_moves", "moves": "top_moves", "smart_money": "top_moves",
    "fund_flows": "fund_flows", "fund": "fund_flows", "13f": "fund_flows",
    "stock_holdings": "stock_holdings", "stock": "stock_holdings", "holdings": "stock_holdings",
    "insider_buys": "insider_buys", "insiders": "insider_buys", "form4": "insider_buys",
}


def _api_base():
    return os.environ.get("APIFY_API_BASE_URL", "https://api.apify.com").rstrip("/")


def _apify_request(url, data=None, method=None, content_type="application/json", timeout=45):
    token = os.environ.get("APIFY_TOKEN")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    last_error = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code == 429 or 500 <= error.code < 600:
                time.sleep(0.75 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            time.sleep(0.75 * (attempt + 1))
    raise last_error


def read_input():
    raw = os.environ.get("APIFY_INPUT_JSON") or os.environ.get("INPUT_JSON")
    if raw:
        return json.loads(raw)
    for env_name in ("APIFY_INPUT_PATH", "ACTOR_INPUT_PATH", "APIFY_ACTOR_INPUT_PATH"):
        path = os.environ.get(env_name)
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as file:
                return json.load(file)
    candidates = glob.glob("/data/key-value-stores/*/INPUT*.json")
    candidates += glob.glob("/data/key-value-stores/*/INPUT")
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            continue
    store_id = os.environ.get("APIFY_DEFAULT_KEY_VALUE_STORE_ID")
    if os.environ.get("APIFY_TOKEN") and store_id:
        url = f"{_api_base()}/v2/key-value-stores/{store_id}/records/INPUT"
        try:
            return json.loads(_apify_request(url, timeout=20).decode("utf-8"))
        except Exception:
            return None
    if len(sys.argv) > 1:
        value = sys.argv[1]
        if os.path.exists(value):
            with open(value, encoding="utf-8") as file:
                return json.load(file)
        try:
            return json.loads(value)
        except Exception:
            return {}
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def _integer(value, default, minimum, maximum):
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _boolean(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _meta(action, status="completed"):
    return {
        "actor": "smart-money-radar",
        "version": ACTOR_VERSION,
        "action": action,
        "status": status,
        "source": "SEC EDGAR (official public 13F-HR and Form 4 filings)",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _error(message, action=None):
    return {
        "record_type": "error",
        "error": message,
        "allowed_actions": list(CANONICAL_ACTIONS),
        "_billing": {"billable_items": 0, "reason": "error"},
        "_pushed_items": 0,
        "_meta": _meta(action, "failed"),
    }


def _runtime_failure(error, action=None):
    if isinstance(error, urllib.error.HTTPError):
        status = int(error.code)
        if status == 403:
            message = ("The SEC upstream service rejected this automated request. Retry later; "
                       "self-hosted clients must set SEC_EDGAR_USER_AGENT to a monitored contact.")
        elif status == 429:
            message = "The SEC upstream rate limit was reached. Retry this run later."
        else:
            message = "The SEC upstream service did not complete this request. Retry later."
    else:
        message = f"Run failed ({type(error).__name__}). Retry later or open a support issue."
    result = _error(message, action)
    result["_meta"].update({
        "error_type": "upstream_http" if isinstance(error, urllib.error.HTTPError) else "runtime",
        "retryable": not isinstance(error, (ValueError,)),
    })
    return result


def run(inp):
    inp = inp or {}
    action = ACTION_ALIASES.get(str(inp.get("action") or "top_moves").lower(),
                                str(inp.get("action") or "top_moves").lower())
    if action not in CANONICAL_ACTIONS:
        return _error(f"Unknown action '{inp.get('action')}'.", action)

    if action == "top_moves":
        min_funds = _integer(inp.get("min_funds"), 2, 2, 25)
        include_etfs = _boolean(inp.get("include_etfs"), False)
        limit = _integer(inp.get("limit"), 50, 1, 200)
        reports = thirteenf.build_fund_reports()
        moves = thirteenf.top_moves(reports, min_funds=min_funds,
                                    include_etfs=include_etfs, limit=limit)
        funds_scanned = [{"slug": r["fund"]["slug"], "name": r["fund"]["name"],
                          "ok": r["qoq"] is not None,
                          "period": r["qoq"]["period"] if r.get("qoq") else None}
                         for r in reports]
        return {
            "record_type": "top_moves",
            "quarter": moves["period"],
            "min_funds_for_convergence": min_funds,
            "include_etfs": include_etfs,
            "funds_scanned": funds_scanned,
            "top_buys": moves["buys"],
            "top_sells": moves["sells"],
            "limitations": [
                "13F data is 45 days delayed; shorts and non-US assets are not disclosed.",
                "Values are quarter-end market values, not trade prices.",
                "Convergence = at least min_funds of the tracked basket moved the same way.",
            ],
            "_meta": _meta(action),
        }

    if action == "fund_flows":
        query = str(inp.get("fund") or "berkshire")
        fund = thirteenf.fund_by_slug_or_cik(query)
        if fund is None:
            name, cik = edgar.resolve_fund(query)
            fund = {"slug": name.lower().replace(" ", "-")[:40], "name": name or query,
                    "manager": None, "cik": cik} if cik else None
        if fund is None:
            return _error(f"Could not resolve fund '{query}'.", action)
        report = thirteenf.fund_report(fund)
        if "error" in report:
            return _error(report["error"], action)
        report["record_type"] = "fund_flows"
        report["_meta"] = _meta(action)
        return report

    if action == "stock_holdings":
        symbol = str(inp.get("symbol") or "").strip()
        if not symbol:
            return _error("Provide 'symbol' (ticker, CUSIP, or issuer name).", action)
        reports = thirteenf.build_fund_reports()
        holdings = thirteenf.stock_holdings(symbol, fund_reports=reports)
        return {
            "record_type": "stock_holdings",
            "symbol": symbol,
            "quarter": thirteenf._latest_period(reports),
            "funds_holding": len(holdings),
            "holdings": holdings,
            "_meta": _meta(action),
        }

    if action == "insider_buys":
        days = _integer(inp.get("days_back"), 30, 1, 365)
        min_value = _integer(inp.get("min_value_usd"), 100_000, 0, 1_000_000_000)
        limit = _integer(inp.get("limit"), 100, 1, 500)
        result = thirteenf.insider_buys(days_back=days, min_value_usd=min_value, limit=limit)
        result["record_type"] = "insider_buys"
        result["_meta"] = _meta(action)
        return result

    return _error(f"Unknown action '{action}'.", action)


def push_to_dataset(items):
    """Push billable rows to the default dataset via the Apify API (best effort)."""
    token = os.environ.get("APIFY_TOKEN")
    dataset_id = os.environ.get("APIFY_DEFAULT_DATASET_ID")
    if not token or not dataset_id:
        return 0
    pushed = 0
    url = f"{_api_base()}/v2/datasets/{dataset_id}/items"
    for item in items:
        payload = json.dumps(item, separators=(",", ":"), default=str).encode("utf-8")
        try:
            _apify_request(url, data=payload, method="POST", timeout=30)
            pushed += 1
        except Exception:
            break
    return pushed


def _dataset_items(result):
    action = result.get("_meta", {}).get("action")
    retrieved_at = result["_meta"]["retrieved_at"]
    if action == "top_moves":
        items = []
        for side, rows in (("buy", result.get("top_buys", [])), ("sell", result.get("top_sells", []))):
            items += [
                {**r, "action": "top_moves", "side": side,
                 "signal": "fund_convergence" if r.get("convergence") else "single_fund_move",
                 "source": "SEC EDGAR 13F-HR", "retrieved_at": retrieved_at}
                for r in rows
            ]
        return items
    if action == "fund_flows":
        return [
            {"record_type": "fund_position_change", "kind": kind, **p,
             "fund": result.get("fund"), "period": result.get("period"),
             "source": "SEC EDGAR 13F-HR", "retrieved_at": retrieved_at}
            for kind in ("new", "increased", "decreased", "exited")
            for p in result.get({"new": "new_positions", "increased": "increased",
                                 "decreased": "decreased", "exited": "exited_positions"}[kind], [])
        ]
    if action == "insider_buys":
        return [
            {**r, "action": "insider_buys", "source": "SEC EDGAR Form 4", "retrieved_at": retrieved_at}
            for r in result.get("insider_buys", [])
        ]
    return [result]


def save_output(result):
    token = os.environ.get("APIFY_TOKEN")
    store_id = os.environ.get("APIFY_DEFAULT_KEY_VALUE_STORE_ID")
    if not token or not store_id:
        return False
    url = f"{_api_base()}/v2/key-value-stores/{store_id}/records/OUTPUT"
    payload = json.dumps(result, separators=(",", ":"), default=str).encode("utf-8")
    _apify_request(url, data=payload, method="PUT", timeout=45)
    return True


def main():
    inp = None
    try:
        inp = read_input()
        result = run(inp)
    except Exception as error:
        raw_action = str((inp or {}).get("action") or "top_moves").lower()
        action = ACTION_ALIASES.get(raw_action, raw_action)
        result = _runtime_failure(error, action)
        print(json.dumps({"event": "actor_runtime_error",
                          "error_type": type(error).__name__,
                          "upstream_status": getattr(error, "code", None)}),
              file=sys.stderr, flush=True)

    if result.get("error"):
        try:
            save_output(result)
        finally:
            print(json.dumps(result, indent=2, default=str))
        raise SystemExit(2)

    try:
        items = _dataset_items(result)
        result["_pushed_items"] = push_to_dataset(items)
        save_output(result)
    except Exception:
        failure = _error("Could not persist the Actor output. Retry later.",
                         result.get("_meta", {}).get("action"))
        failure["_meta"].update({"error_type": "persistence", "retryable": True})
        print(json.dumps(failure, indent=2, default=str))
        raise SystemExit(3)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
