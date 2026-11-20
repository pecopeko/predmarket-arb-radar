# PredMarket Arb Radar

**Live Read-Only Cross-Platform Prediction Market Arbitrage, Multi-Outcome Dutch-Book Audit, and Probability Divergence Radar** across **Polymarket** (Gamma + CLOB), **Kalshi** (Public Trade API v2), and **Manifold Markets**.

- **Live Dashboard (GitHub Pages):** [https://pecopeko.github.io/predmarket-arb-radar/](https://pecopeko.github.io/predmarket-arb-radar/)
- **Live JSON Feed:** [https://pecopeko.github.io/predmarket-arb-radar/latest_scan.json](https://pecopeko.github.io/predmarket-arb-radar/latest_scan.json)
- **Whop + Discord Monetization Playbook:** [WHOP_DISCORD_LAUNCH_PLAYBOOK.md](./WHOP_DISCORD_LAUNCH_PLAYBOOK.md)

> **Strictly Read-Only & `$0.00` Infrastructure:** Uses only public unauthenticated market-data APIs. Zero custody, zero wallet funding, zero order execution, zero paid API keys.

---

## Why This Radar Is Different From Naive Arb Scanners

1. **Separates True Exhaustive Dutch-Books From `negRiskAugmented` Traps**
   - Naive scanners sum active YES asks on Polymarket's 2028 election markets (`~$0.953`) and scream *"4.7% risk-free arb!"* — ignoring `negRiskAugmented: true`, inactive placeholder slots, and excluded `negRiskOther` tail risk.
   - PredMarket Arb Radar audits `negRisk`, `negRiskAugmented`, inactive placeholder counts, and Kalshi `mutually_exclusive` / open-field coverage, explicitly classifying non-exhaustive sub-`$1.00` baskets as **`NON_EXHAUSTIVE_AUGMENTED_TRAP`**.
2. **Exact 2026 Exchange Taker Fee Formulas**
   - **Kalshi:** `ceil_cent(0.07 * C * P * (1 - P))` (marginal `0.07 * P * (1 - P)`, max `$0.0175/contract` at `$0.50`).
   - **Polymarket:** Reads live `feesEnabled` and `feeSchedule.rate` per contract from Gamma API and computes `rate * P * (1 - P)` (e.g., `0.04` politics/finance, `0.05` economics/sports, `0.07` crypto, `0.00` fee-free markets).
3. **Real Live Orderbook Depth Verification (Not Stale Midpoints)**
   - Queries Polymarket CLOB `/book?token_id=...` (explicitly sorting asks *ascending* since Polymarket's CLOB returns asks descending!) and Kalshi `/markets/{ticker}/orderbook` to compute exact **orderbook-capped fillable shares, USD capacity, and expected net profit**.
4. **Strict False-Match Rejection**
   - Hard-rejects cross-venue pairs with mismatched target years, mismatched meeting/expiry months, mismatched numeric strikes (`$100k` vs `$120k`), opposite directions (`above` vs `below`), mismatched candidate labels, or opposite polarity (`"out of power"` vs `"remain President"`).
5. **Honest Venue Currency & Risk Labeling**
   - Manifold Markets is explicitly labeled `MANA_PLAY_MONEY` and surfaced strictly as a crowd-calibration **Probability Divergence** benchmark — never mixed into USD cash arbitrage calculations.
   - Every cash-eligible cross-venue opportunity surfaces **Annualized Net ROI %**, **Days to Expiry**, `LEGGED_IN_EXECUTION_RISK`, and `RESOLUTION_ORACLE_BASIS_RISK`.

---

## Architecture

```
Polymarket Gamma + CLOB (USD) ──┐
Kalshi Public Trade API v2 (USD)┼─► venues.py ─► arb_engine.py ─┬─► dashboard_builder.py ─► docs/index.html + latest_scan.json
Manifold Markets API v0 (Mana) ─┘                               └─► alerter.py (Dedup) ───► Discord / Telegram Webhooks
```

- `src/predmarket_radar/venues.py`: Polite, rate-limited `httpx` fetchers with honest `VenueStatus` provenance & graceful multi-venue degradation.
- `src/predmarket_radar/arb_engine.py`: Fee engine, Dutch-book exhaustiveness auditor, strict proposition normalizer & matcher, and live CLOB/Kalshi book enricher.
- `src/predmarket_radar/alerter.py`: Discord rich embed + Telegram HTML formatter with persistent JSON deduplication cooldown (`AlertDeduplicator`) and dry-run-safe dispatcher.
- `src/predmarket_radar/dashboard_builder.py`: Orchestrates live scans and generates the self-contained, XSS-safe static HTML radar dashboard (`docs/index.html`, `docs/latest_scan.json`, `docs/.nojekyll`).
- `.github/workflows/radar_refresh.yml`: Free GitHub Actions cron running every 30 minutes to refresh GitHub Pages and fire optional `DISCORD_WEBHOOK_URL` alerts.

---

## Quickstart (Python 3.11+)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run unit & smoke test suite
pytest -v

# Run a live read-only scan and print summary JSON
predmarket-radar scan

# Rebuild the static HTML dashboard + latest_scan.json (and dry-run Discord alerts)
predmarket-radar build-dashboard --docs-dir docs --alert
```

To enable live Discord webhook delivery (optional):
```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
predmarket-radar build-dashboard --docs-dir docs --alert
```

---

## Risk & Non-Advisory Disclaimer

This software provides **informational market data only** and does **not** constitute financial, investment, trading, or legal advice. Cross-venue prediction market trades carry non-atomic **legged-in execution risk**, **settlement/oracle rule basis risk** (e.g., Polymarket UMA vs Kalshi CFTC rulebooks), and **capital lockup risk**. Always verify live orderbooks and exact contract resolution criteria directly on each exchange.

## License

MIT — see [LICENSE](./LICENSE).
