# Whop + Discord Launch Playbook (`$0.00` Out-of-Pocket Subscription Engine)

This playbook documents how to monetize **PredMarket Arb Radar** as a **$0.00-startup, zero-custody, read-only market-intelligence subscription** using **GitHub Pages (public lead magnet)** + **Whop (0 upfront fee, pay-as-you-earn checkout & role automation)** + **Discord (private tiered alert channels)** + **GitHub Actions (free automated refresh & webhook engine)**.

> **Core Positioning:** We never touch subscriber funds, never require exchange API keys, and never execute trades. Subscribers pay for **clean, fee-adjusted, orderbook-verified cross-venue spread data** and **exhaustiveness trap filtering** (saving them hours of manual orderbook checks and preventing costly `negRiskAugmented` mistakes).

---

## 1. Zero-Cost Infrastructure Stack (`$0.00/mo` Fixed Overhead)

| Layer | Service | Monthly Cost | Role |
| :--- | :--- | :--- | :--- |
| **Live Public Funnel** | GitHub Pages (`main/docs`) | `$0.00` | Live interactive radar at `https://pecopeko.github.io/predmarket-arb-radar/` |
| **Scan & Alert Compute** | GitHub Actions Cron (`17,47 * * * *`) | `$0.00` | Public repo unlimited/free standard runner refreshes + Discord webhook dispatch |
| **Market Data** | Polymarket Gamma + CLOB, Kalshi Public Trade API v2, Manifold v0 | `$0.00` | Strictly public read-only unauthenticated GET endpoints |
| **Billing & Tax Handling** | Whop (Merchant of Record + native Discord Bot) | `$0.00` upfront (Whop takes % fee only on completed sales) | Recurring subscription billing, automated role assignment & revocation |
| **Signal Delivery** | Private Discord Server + Optional Telegram Channel | `$0.00` | Tier-gated webhook channels (`#arb-alerts-pro`, `#quant-desk-depth`, `#trap-warnings`) |

---

## 2. Subscription Tier Architecture & Pricing Ladder

| Tier | Price | Whop Role | Deliverables | Target Subscriber |
| :--- | :--- | :--- | :--- | :--- |
| **Free Public Radar** | `$0` | `@Radar-Free` (optional) | Public GitHub Pages dashboard (30-min refresh), open-source scanner repo, Discord `#announcements` + weekly top-spread recap | Curious Polymarket/Kalshi traders discovering the tool from X/Reddit |
| **Pro Signal Feed** | **`$19/mo`** (`$180/yr`) | `@Radar-Pro` | Instant deduplicated Discord embeds for **Pure Fee-Adjusted Cross-Venue Arbs (`Net ROI > 0%`)** + **High-Liquidity Divergences (`≥ 5.0pp`)** across Polymarket, Kalshi, and Manifold | Active retail event traders & arbitrage hunters |
| **Quant Desk** | **`$49/mo`** (`$468/yr`) | `@Radar-Desk` | Everything in Pro + **CLOB & Kalshi Orderbook Depth-Capped Sizing (`Max USD`, `Est. Net PnL`)**, **Sub-$1.00 Maker Near-Arb alerts (`0%` maker fee edge)**, **Annualized ROI / Capital-Lockup filters**, and downloadable `latest_scan.json` / CSV history | Serious multi-venue traders & small quant pods |
| **Elite Custom Feed** | **`$79/mo`** (`$99/mo` w/ dedicated watchlist tuning) | `@Radar-Elite` | Everything in Desk + **Private Webhook Forwarding** (send directly to subscriber's own Discord/Telegram), custom Kalshi series & Polymarket event watchlists, custom slippage-bps thresholds, and resolution-rule basis-risk notes | High-volume event-market makers & researchers |

*Unit Economics at `$0.00` fixed overhead:*
- **10 Pro + 4 Desk + 2 Elite subscribers** = `$190 + $196 + $158` = **`$544/mo` gross MRR** (~`$510+/mo` net after Whop fees) with **$0 server bills**!

---

## 3. Step-by-Step Whop + Discord Role-Gating Setup (15 Minutes)

### Step A: Create the Discord Server & Channel Architecture
1. Create a clean Discord server named **PredMarket Arb Desk**.
2. Create four channel groups with strict role permissions (`@everyone` = Read-Only on public, Deny View on paid):
   - **PUBLIC**: `#start-here-disclaimer`, `#live-radar-link`, `#weekly-recap`
   - **PRO (`@Radar-Pro`)**: `#pure-cash-arbs`, `#divergence-5pp`, `#dutch-book-traps`
   - **DESK (`@Radar-Desk`)**: `#maker-near-arbs`, `#orderbook-depth-feed`, `#json-snapshots`
   - **ELITE (`@Radar-Elite`)**: `#custom-watchlist-requests`, `#basis-risk-notes`
3. Create dedicated **Discord Channel Webhooks**:
   - Channel Settings → Integrations → Webhooks → New Webhook → name it `PredMarket Arb Radar` → copy Webhook URL.

### Step B: Connect GitHub Actions to Discord (Zero Server Required)
1. Open your GitHub repository → **Settings → Secrets and variables → Actions**.
2. Add repository secret:
   - `DISCORD_WEBHOOK_URL` = your `#pure-cash-arbs` Discord webhook URL.
   - *(Optional)* `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for mirrored Telegram delivery.
3. Optional repository variables (`Variables` tab):
   - `RADAR_SLIPPAGE_BPS` = `0.0` (or `10.0` for conservative 10 bps slippage buffer)
   - `RADAR_MIN_DIVERGENCE_PP` = `3.0`
   - `RADAR_ALERT_COOLDOWN_SEC` = `21600` (6-hour dedup window; re-alerts early if edge widens by `+100 bps`)
4. Trigger `.github/workflows/radar_refresh.yml` manually once via **Actions → Run workflow** to verify live embed delivery!

### Step C: Launch the `$0.00` Whop Storefront
1. Sign up free at `https://whop.com/sell` and create a Whop Company (**PredMarket Arb Radar**).
2. Add the **Discord App** inside your Whop dashboard and authorize Whop's bot in your Discord server (place the Whop Bot role *above* `@Radar-Pro`, `@Radar-Desk`, and `@Radar-Elite` in Discord Server Settings → Roles!).
3. Create three recurring subscription products on Whop:
   - **Pro Signal Feed — `$19/mo`** → grants `@Radar-Pro` automatically on checkout and revokes automatically on cancellation.
   - **Quant Desk — `$49/mo`** → grants `@Radar-Pro` + `@Radar-Desk`.
   - **Elite Custom Feed — `$79/mo`** → grants `@Radar-Pro` + `@Radar-Desk` + `@Radar-Elite`.
4. Link your live public GitHub Pages dashboard (`https://pecopeko.github.io/predmarket-arb-radar/`) directly on your Whop storefront page as a live proof-of-work preview!

---

## 4. Transparent Legal, Regulatory & Risk Disclaimers (Required on Whop, Discord & Dashboard)

Place this notice prominently in `#start-here-disclaimer`, your Whop Terms of Service box, and every webhook footer:

> **Informational Data Only — Not Financial, Investment, or Legal Advice.**
> PredMarket Arb Radar is a strictly read-only public market-data aggregation and analytics tool. We do not execute trades, manage funds, hold custody of assets, broker orders, or recommend buying or selling any event contract.
>
> **Key Structural Risks Every User Must Independently Verify:**
> 1. **Legged-In Execution Risk:** Cross-venue arbitrage between Polymarket and Kalshi is **non-atomic**. One leg may fill while the opposite book moves or disappears.
> 2. **Resolution & Oracle Basis Risk:** Polymarket resolves via UMA Optimistic Oracle and specific market rules; Kalshi resolves as a CFTC-regulated Designated Contract Market under its own rulebook. Subtle wording differences (e.g., `>25bps` vs `50+ bps`, midnight ET cutoffs, or different reference price indices) can cause diverging settlements.
> 3. **Capital Lockup & Opportunity Cost:** A `+1.3%` net spread expiring in November 2028 yields `< 0.7%` annualized — always compare against risk-free Treasury yields (`annualized_net_roi_pct`).
> 4. **Non-Exhaustive Baskets:** Polymarket `negRiskAugmented=true` events with sum-of-asks `< $1.00` are **not** risk-free arbitrage because unlisted candidates / `Other` can settle YES.
> 5. **Jurisdiction Compliance:** Users are solely responsible for ensuring prediction market access complies with their local laws and venue Terms of Service.

---

## 5. Organic `$0.00` Distribution Copy (X / Twitter & Reddit Showcase)

### X (Twitter) Launch Thread (3-Tweet Proof-First Hook)
**Tweet 1:**
> Most "Polymarket arbitrage bots" on X scream about fake 4.7% risk-free arbs on the 2028 Dem Nominee basket (`sum of YES asks = $0.953`).
>
> Why it's a trap: `negRiskAugmented = true` + 70 inactive placeholder slots means an unlisted candidate/"Other" kills your hedge.
>
> So I built a live, open-source, read-only Polymarket + Kalshi + Manifold spread & Dutch-book audit radar — free live link below 🧵👇

**Tweet 2:**
> What the live scanner actually audits every 30 min (zero fake numbers):
> • Exact Kalshi taker fee `ceil(0.07 * C * P * (1-P))` + live Polymarket Gamma `feeSchedule.rate * P * (1-P)`
> • Live CLOB + Kalshi orderbook depth cap (no phantom $0.001 quotes — Polymarket's CLOB returns asks descending, which breaks naive parsers!)
> • Real annualized ROI so you don't lock capital for 2 years for +0.5%
> • Strict entity/year/threshold/polarity rejection (blocks mismatched dates & inverted questions)

**Tweet 3:**
> Live public radar (refreshed automatically via GitHub Actions, 100% open-source MIT):
> 🔗 https://pecopeko.github.io/predmarket-arb-radar/
> 💻 https://github.com/pecopeko/predmarket-arb-radar
>
> If you want instant deduplicated Discord alerts when real fee-adjusted Polymarket ↔ Kalshi spreads or ≥5pp divergences open up, Pro/Desk Discord access is live via Whop (zero custody, informational data only).

### Reddit Showcase Post (`r/algotrading`, `r/PredictionMarkets`, `r/Polymarket`)
**Title:** `I built an open-source, read-only Polymarket + Kalshi + Manifold arbitrage & Dutch-book trap radar (with live CLOB depth & exact 2026 taker fee math)`

**Body:**
> I noticed two recurring bugs in public prediction-market arb scanners:
> 1. **Fake Dutch-books on `negRiskAugmented=true` markets:** Summing active YES asks on Polymarket's 2028 election markets gives `~$0.953`, which naive scanners flag as a 4.7% risk-free arb while ignoring the 70 inactive placeholder slots and excluded `negRiskOther` tail risk.
> 2. **Broken fee & CLOB parsing:** Polymarket's `/book` endpoint returns `asks` sorted *descending* (highest price first) and now enforces category-specific taker fees (`feeSchedule.rate * p * (1 - p)`), while Kalshi charges `0.07 * p * (1 - p)`.
>
> I put together a strictly read-only Python engine + live static dashboard (`https://pecopeko.github.io/predmarket-arb-radar/`) that scans Polymarket (Gamma + CLOB), Kalshi Public Trade API v2, and Manifold Markets (flagged explicitly as play-money Mana divergence benchmark only — never mixed into USD cash arbs).
>
> Source code (MIT): `https://github.com/pecopeko/predmarket-arb-radar`
>
> Happy to take feedback on additional Kalshi series tickers or resolution-rule basis-risk edge cases to catch!
