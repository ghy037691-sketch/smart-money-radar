/* Smart Money Radar — client logic. */
"use strict";

function fmtUSD(n) {
  if (n == null) return "—";
  const neg = n < 0; n = Math.abs(n);
  let s;
  if (n >= 1e9) s = "$" + (n / 1e9).toFixed(2).replace(/\.?0+$/, "") + "B";
  else if (n >= 1e6) s = "$" + (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  else if (n >= 1e3) s = "$" + (n / 1e3).toFixed(0) + "K";
  else s = "$" + Math.round(n);
  return (neg ? "−" : "") + s;
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function j(url, opts) {
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 202) throw new Error("WARMING");
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

/* ---------- moves ---------- */
function fundChips(funds, side) {
  const list = funds
    .filter(f => (side === "buy" ? f.delta_usd > 0 : f.delta_usd < 0))
    .sort((a, b) => Math.abs(b.delta_usd) - Math.abs(a.delta_usd));
  return '<div class="fund-chips">' + list.map(f => {
    const v = fmtUSD(Math.abs(f.delta_usd));
    return `<span title="${esc(f.fund)} ${v}">${esc((f.fund.split(" ")[0] || f.fund))}${f.is_new ? " ★new" : ""}</span>`;
  }).join("") + "</div>";
}
function moveRow(r, side) {
  const name = r.ticker ? `${r.ticker} · ${esc(r.issuer)}` : esc(r.issuer);
  const cls = r.class ? `<span class="sec-class">${esc(r.class)}</span>` : "";
  const badges =
    (r.convergence ? `<span class="badge conv">${r.funds_buying + r.funds_selling} funds</span> ` : "") +
    (r.is_new_anywhere ? `<span class="badge new">new pos</span> ` : "") +
    (r.etf ? `<span class="badge etf">ETF</span>` : "");
  const cnt = side === "buy" ? r.funds_buying : r.funds_selling;
  return `<tr>
    <td><span class="sec-name">${name}</span><br>${cls}${badges ? "<br>" + badges : ""}</td>
    <td class="num"><b>${cnt}</b></td>
    <td class="num ${side === "buy" ? "up" : "down"}">${fmtUSD(r.net_delta_usd)}</td>
    <td>${fundChips(r.funds, side)}</td>
  </tr>`;
}
let movesTimer = null;
async function loadMoves() {
  const incEtf = document.getElementById("etf-toggle").checked ? "1" : "";
  try {
    const d = await j("/api/moves?limit=25" + (incEtf ? "&include_etfs=1" : ""));
    document.getElementById("quarter-badge").textContent =
      "Official SEC Form 13F · " + (d.quarter ? "quarter ended " + d.quarter : "") +
      (incEtf ? " · incl. ETFs" : "");
    document.getElementById("moves-quarter").textContent = d.quarter ? `(quarter ended ${d.quarter})` : "";
    document.querySelector("#buys-table tbody").innerHTML =
      d.buys.map(r => moveRow(r, "buy")).join("") || '<tr><td colspan="4" class="muted">No net buys recorded.</td></tr>';
    document.querySelector("#sells-table tbody").innerHTML =
      d.sells.map(r => moveRow(r, "sell")).join("") || '<tr><td colspan="4" class="muted">No net sells recorded.</td></tr>';
  } catch (e) {
    if (e.message === "WARMING" && !movesTimer) {
      document.querySelector("#buys-table tbody").innerHTML =
        '<tr><td colspan="4" class="muted">⏳ Fetching this quarter\'s 13F filings from SEC EDGAR (first load ~1-2 min) — retrying…</td></tr>';
      movesTimer = setTimeout(() => { movesTimer = null; loadMoves(); }, 10000);
    }
  }
}
document.getElementById("etf-toggle").addEventListener("change", loadMoves);

/* ---------- funds ---------- */
async function loadFunds() {
  try {
    const d = await j("/api/funds");
    const grid = document.getElementById("fund-grid");
    grid.innerHTML = d.funds.map(f => {
      const topNew = (qoqTopNew[f.slug] || []).map(t => t.ticker || t.issuer.split(" ")[0]);
      return `<div class="card fund-card" data-slug="${f.slug}">
        <h4>${esc(f.name)}</h4>
        <div class="mgr">${esc(f.manager || "—")}</div>
        <div class="fund-stat"><span>Portfolio (QoE)</span><b>${fmtUSD(f.total_value_usd)}</b></div>
        <div class="fund-stat"><span>Positions</span><b>${f.positions_count != null ? f.positions_count.toLocaleString() : "—"}</b></div>
        <div class="fund-stat"><span>New / Exited</span><b>${f.new_count} / ${f.exited_count}</b></div>
        ${f.error ? `<div class="fund-stat"><span>Status</span><b class="down">unavailable</b></div>` : ""}
      </div>`;
    }).join("");
    grid.querySelectorAll(".fund-card").forEach(card =>
      card.addEventListener("click", () => loadFundDetail(card.dataset.slug)));
  } catch (e) {
    document.getElementById("fund-grid").innerHTML =
      '<p class="muted">⏳ Fund data warming up — retrying shortly…</p>';
    setTimeout(loadFunds, 10000);
  }
}
const qoqTopNew = {};
async function loadFundDetail(slug) {
  const box = document.getElementById("fund-detail");
  box.hidden = false;
  box.innerHTML = '<p class="muted">Loading full report…</p>';
  try {
    const d = await j("/api/fund?slug=" + encodeURIComponent(slug));
    if (!d || !d.period) { box.innerHTML = '<p class="muted">Fund data not ready yet.</p>'; return; }
    qoqTopNew[slug] = d.new_positions || [];
    const row = (p, extra = "") => `<tr>
      <td><b>${esc(p.ticker || "—")}</b> · ${esc(p.issuer)} ${p.put_call ? `<span class="badge etf">${esc(p.put_call)}</span>` : ""}</td>
      <td class="num">${fmtUSD(p.value_usd)}</td>
      <td class="num">${p.shares != null ? p.shares.toLocaleString() : "—"}</td>
      <td class="num">${p.pct_of_portfolio != null ? p.pct_of_portfolio + "%" : ""}</td>
      <td>${extra}</td></tr>`;
    const section = (title, list, deltaKey, cls) => list.length ? `
      <h3>${title}</h3>
      <table class="detail-table"><thead><tr><th>Security</th><th>Value</th><th>Shares</th><th>% of book</th><th>Δ</th></tr></thead>
      <tbody>${list.slice(0, 10).map(p => row(p, p[deltaKey] != null ? `<span class="${cls(p[deltaKey])}">${fmtUSD(Math.abs(p[deltaKey]))}</span>` : "")).join("")}</tbody></table>` : "";
    box.innerHTML = `
      <h3>${esc(d.fund)} <span class="muted small">${esc(d.manager || "")} · period ${esc(d.period)} · ${fmtUSD(d.total_value_usd)}</span></h3>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">
      <div>${section("Top positions", d.top_positions || [], null, () => "")}</div>
      <div>
        ${section("★ New positions this quarter", d.new_positions || [], "value_usd", v => "up")}
        ${section("▲ Increased", (d.increased || []).slice(0, 8), "delta_usd", v => "up")}
        ${section("▼ Decreased", (d.decreased || []).slice(0, 8), "delta_usd", v => "down")}
        ${section("✕ Exited", (d.exited_positions || []).slice(0, 8), "delta_usd", v => "down")}
      </div></div>
      ${d.source_url ? `<p class="muted small">Source: <a href="${esc(d.source_url)}" target="_blank" rel="noopener">SEC filing ↗</a></p>` : ""}`;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    box.innerHTML = '<p class="muted">Could not load fund report — retry shortly.</p>';
  }
}

/* ---------- insider buys ---------- */
async function loadInsider() {
  try {
    const d = await j("/api/insider?days=30&min_value=100000&limit=60");
    const body = document.querySelector("#insider-table tbody");
    body.innerHTML = d.insider_buys.map(r => `<tr>
      <td><b>${esc(r.company)}</b>${r.ticker ? ` <span class="muted small">${esc(r.ticker)}</span>` : ""}<br>
          <span class="muted small">${esc(r.roles.slice(0, 2).join(", ") || "insider")}</span></td>
      <td>${esc(r.insider)}</td>
      <td class="num">${esc(r.date)}</td>
      <td class="num">${r.shares != null ? Number(r.shares).toLocaleString() : "—"}</td>
      <td class="num up">${fmtUSD(r.value_usd)}</td>
      <td>${r.cluster ? '<span class="badge cluster">CLUSTER BUY</span>' : '<span class="muted small">buy</span>'}</td>
    </tr>`).join("") || '<tr><td colspan="6" class="muted">No qualifying buys in window.</td></tr>';
    const n = d.clusters_found;
    if (n) document.querySelector("#insiders .lead").innerHTML +=
      ` <b class="gold">${n} cluster${n > 1 ? "s" : ""} detected</b> in the last 30 days.`;
  } catch (e) {
    const body = document.querySelector("#insider-table tbody");
    body.innerHTML = '<tr><td colspan="6" class="muted">Insider data warming up — retrying in 15s…</td></tr>';
    setTimeout(loadInsider, 15000);
  }
}

/* ---------- stock lookup ---------- */
document.getElementById("stock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = document.getElementById("stock-q").value.trim();
  const box = document.getElementById("stock-result");
  if (!q) return;
  box.hidden = false;
  box.innerHTML = '<p class="muted">Checking 17 fund books…</p>';
  try {
    const d = await j("/api/stock?q=" + encodeURIComponent(q));
    if (!d.holdings.length) {
      box.innerHTML = `<p>No holdings of <b>${esc(q)}</b> found in the tracked basket this quarter.
      <span class="muted">(Try a ticker like TSLA, NVDA, AAPL — or check the spelling.)</span></p>`;
      return;
    }
    box.innerHTML = `<div class="card"><h3>Who's holding <span class="accent">${esc(q)}</span></h3>` +
      d.holdings.map(h => `<div class="stock-row">
        <span class="st">${esc(h.ticker || "—")}</span>
        <span class="name">${esc(h.fund)}${h.manager ? ` <span class="muted">· ${esc(h.manager)}</span>` : ""}</span>
        <span class="val">${fmtUSD(h.value_usd)}</span>
        <span class="muted small">${esc(h.movement)}${h.pct_of_portfolio != null ? " · " + h.pct_of_portfolio + "%" : ""}</span>
      </div>`).join("") + "</div>";
  } catch (err) {
    box.innerHTML = err.message === "WARMING"
      ? '<p class="muted">Data is warming up — try again in a minute.</p>'
      : '<p class="muted">Lookup failed — retry shortly.</p>';
  }
});

/* ---------- tabs (scrollspy-ish) ---------- */
document.querySelectorAll("#tabs .tab").forEach(t => {
  t.addEventListener("click", () => {
    document.querySelectorAll("#tabs .tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
  });
});
["moves", "funds", "insiders"].forEach(id => {
  const el = document.getElementById(id);
  new IntersectionObserver(entries => {
    if (entries[0].isIntersecting) {
      document.querySelectorAll("#tabs .tab").forEach(x =>
        x.classList.toggle("active", x.dataset.tab === id));
    }
  }, { rootMargin: "-40% 0px -50% 0px" }).observe(el);
});

loadMoves();
loadFunds();
loadInsider();
