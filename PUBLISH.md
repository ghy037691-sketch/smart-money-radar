# Apify Store Listing — Smart Money Radar

Ready-to-paste copy for the Actor Store listing (mirror of `edgar-signals` publishing flow).

## Name
`smart-money-radar`

## Title
Smart Money Radar — 13F Fund Flows & Insider Buys

## Description (short)
Cross-fund institutional flow signals from official SEC filings. See which stocks Berkshire, Citadel, Tiger, Bridgewater, Pershing, D1 and 12 more funds are all buying or selling this quarter (13F convergence), drill into any fund's QoQ changes, check which tracked funds hold any security, and scan recent large open-market insider purchases with cluster-buy detection (Form 4). No API key required.

## Marketing text (long)

**The "what did Buffett buy this quarter" signal — as data.**

Every quarter, institutions managing $100M+ must file their complete US equity
positions (Form 13F) with the SEC. Most tools parse **one fund at a time**.
This Actor aggregates **17 marquee funds at once** and answers the question
that actually matters: *which stocks are several top funds moving on in the
same quarter?*

### What you get

- **`top_moves` (default)** — the latest 13F quarter across a verified 17-fund
  basket (Berkshire, Bridgewater, Renaissance, Citadel, Tiger Global,
  Appaloosa, Pershing Square, Greenlight, D1, Baillie Gifford, Duquesne
  Family Office, Third Point, Point72, Soros, ValueAct, Starboard, Coatue).
  Each row: security, ticker, net $ change, number of funds buying/selling,
  per-fund deltas, convergence flag, new-position flag. ETFs filtered by
  default (passive allocation ≠ stock-picking).
- **`fund_flows`** — one fund's latest 13F: top positions with portfolio
  weights + QoQ new / increased / decreased / exited.
- **`stock_holdings`** — "who holds NVDA?" across all 17 books, with
  per-fund value, weight, and movement label. Ticker, CUSIP, or issuer name.
- **`insider_buys`** — recent open-market Form 4 purchases above a value
  threshold, with **cluster-buy detection** (≥2 distinct insiders buying the
  same company in the window).

### Honest limitations (shown in output)

- 13F data is 45 days delayed; values are quarter-end marks, not trade prices.
- Only long US equity positions are disclosed — shorts and non-US assets never appear.
- Funds that haven't filed for the newest quarter are excluded from `top_moves`
  (each fund's quarter is visible in the fund list).

### Pricing (suggested, matches category)

| Event | Price |
|---|---|
| Actor start | $0.001 |
| 13F filing parsed | $0.010 |
| Row returned | $0.001 |

A default `top_moves` run (17 filings) ≈ $0.25; `fund_flows` for a 200-position
fund ≈ $0.30. Errors produce zero-billable rows (see billing model in code).

## Keywords
sec, edgar, 13f, hedge fund, portfolio, buffett, berkshire, tiger global,
insider, form 4, cluster buy, stock holdings, qoq, convergence, smart money
