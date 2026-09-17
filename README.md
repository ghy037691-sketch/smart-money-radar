# Smart Money Radar

**See what the smartest funds are buying — and selling — this quarter.**
Cross-fund institutional flow signals from official SEC filings: 17 marquee funds' latest Form 13F, parsed and aggregated into convergence signals (several funds buying the same stock in the same quarter), plus large open-market insider purchases with cluster-buy detection from Form 4.

Live: **https://smart-money-radar.onrender.com**
Apify Actor: `smart-money-radar` (this repo builds it — see [PUBLISH.md](PUBLISH.md) for the Store listing copy)

## Why this is a money product

- **Demand is proven.** "What did Buffett buy this quarter" is a perennial search pattern; SeekingAlpha, WhaleWisdom, Insider Monkey and HedgeTrace all run on exactly this data. HedgeTrace's "Smart Money Moves" pages are their highest-traffic content.
- **The gap:** existing Apify actors parse *single* funds. Nobody on the marketplace offers the **cross-fund convergence view** — "3 of 17 tracked funds all added NVIDIA this quarter" — which is the actually-actionable signal and the most linkable, embeddable, newsletter-able output.
- **Quarterly pulse = recurring traffic.** Every 45 days new data lands, re-activating the same audience.
- **Two revenue surfaces, one codebase:** free web (traffic + lead capture) and a billable Apify Actor (per-filing/per-row pricing like the proven `edgar-signals`).

## What it does

| Surface | Output |
|---|---|
| Web — Smart Money Moves | Top buys/sells across the basket: net $ change, # funds moving, convergence badge, per-fund chips |
| Web — Funds | Per fund: portfolio value, position count, new/exited vs prior quarter, full QoQ report |
| Web — Insider Buys | Large open-market Form 4 purchases (30d), cluster-buy flagging |
| Web — Stock lookup | "Who's holding TSLA" across all 17 books, with movement label |
| Actor `top_moves` | Cross-fund convergence rows (billable dataset rows) |
| Actor `fund_flows` | One fund: top positions + QoQ new/increased/decreased/exited |
| Actor `stock_holdings` | Which tracked funds hold a security |
| Actor `insider_buys` | Insider purchase scan with cluster detection |

**Tracked basket (17, every CIK verified live 2026-09-17):** Berkshire (Buffett), Bridgewater (Dalio), Renaissance (Simons), Citadel (Griffin), Tiger Global (Price), Appaloosa (Mnuchin), Pershing Square (Ackman), Greenlight (Einhorn), D1 (Tepper), Baillie Gifford (Shane), Duquesne Family Office, Third Point (Cohen), Point72 (Cohen), Soros Fund Management, ValueAct (Allen), Starboard, Coatue (Choi).

## Data honesty (also shown on-site)

- 13F data is **45 days delayed**; values are quarter-end marks, not trade prices.
- Only long US equity positions are disclosed — shorts and non-US assets never appear.
- CUSIP→ticker via the SEC's own `company_tickers.json` name matching (no paid keys); unmatched positions show issuer name only.
- ETFs are flagged and excluded by default (passive allocation ≠ stock-picking signal).
- ETF/sell signals from 13F are directional hints, **not investment advice**.

## Run locally

Requirements: Python 3.10+. **Zero dependencies.**

```bash
python3 -m py_compile main.py server.py src/edgar.py src/thirteenf.py src/names.py
python3 -m unittest discover -s tests -v
PORT=8000 python3 server.py          # web (first build ~1-2 min, then disk-cached 24h)
echo '{"action":"fund_flows","fund":"citadel"}' | python3 main.py   # actor, local
```

Env vars: `PORT` (default 8000), `SEC_EDGAR_USER_AGENT` (set a monitored contact for self-hosted use).

## Money model

1. **Web = traffic.** Quarterly "smart money moves" pages are naturally shareable; every row links to its source filing (trust + SEO).
2. **Lead capture.** "Want this as a private feed / buyer-intro report?" form (same pattern as SaaS Multiples) → manual premium sales first (₹/USD via UPI/PayPal/bank), gateway later.
3. **Apify Actor = the proven channel.** Pricing mirrors the category leader: start $0.001 · $0.01/filing parsed · $0.001/holding row. `top_moves` default run ≈ 17 filings → cheap to start, scales with usage.
4. **Buyer side.** Portfolio buyers (saas.group/iTrinity-style and hedge-fund scouts) pay for curated "what's moving" digests — the same data, packaged differently.

## Repo map

```
main.py             Apify Actor entry (4 actions, billable dataset rows, OUTPUT kv)
server.py           Render web: cached basket, public JSON API, stock/insider endpoints
src/edgar.py        SEC EDGAR client (UA, throttle, retries, full-text search)
src/thirteenf.py    13F fetch/parse/QoQ diff/cross-fund aggregation, insider scan
src/names.py        issuer -> ticker via SEC company_tickers.json (cached 7d)
src/funds.json      17 verified funds
web/                single-page dark UI (no build step, no external assets)
.actor/             Apify schemas (input/output/dataset) + actor.json
Dockerfile          Apify actor image (apify/actor-python:3.12)
Dockerfile.web      Render image (python:3.12-slim)
tests/              engine fixtures + full HTTP API integration tests
```

## Known limits / next

- Free Render = ephemeral disk: the 24h basket cache dies on redeploy (first load rebuilds ~1-2 min). Durable Postgres cache is the next infra step.
- 13F basket is fixed at 17 (easy to extend in `src/funds.json` — CIKs must be verified live first).
- No auth/paywall yet — that's the v2 (Cashfree/Stripe env + premium endpoints).
