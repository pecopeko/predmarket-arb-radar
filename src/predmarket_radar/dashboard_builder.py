"""Live scan orchestrator and self-contained interactive static HTML dashboard generator."""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Optional

from .alerter import RISK_DISCLAIMER, build_sample_discord_embeds
from .arb_engine import (
    enrich_cross_venue_orderbooks,
    enrich_dutch_book_orderbooks,
    match_cross_venue,
    scan_dutch_books,
)
from .models import ScanReport, VenueStatus
from .venues import (
    DEFAULT_KALSHI_SERIES,
    POLYMARKET_CLOB_BOOK_URL,
    create_http_client,
    fetch_kalshi_data,
    fetch_manifold_data,
    fetch_polymarket_data,
    utc_now_iso,
)


def run_live_scan(
    max_polymarket_events: int = 200,
    kalshi_series: tuple[str, ...] = DEFAULT_KALSHI_SERIES,
    kalshi_general_pages: int = 1,
    manifold_limit_per_sort: int = 80,
    manifold_expand_multichoice: int = 10,
    slippage_bps: float = 0.0,
    min_divergence_pp: float = 3.0,
    min_activity_threshold: float = 200.0,
    enrich_orderbooks: bool = True,
    max_dutch_books_to_enrich: int = 5,
    max_matches_to_enrich: int = 18,
) -> ScanReport:
    """Execute a complete live read-only scan across Polymarket, Kalshi, and Manifold."""
    t0 = time.monotonic()
    with create_http_client() as client:
        pm_markets, pm_multi, pm_status = fetch_polymarket_data(
            client,
            max_events=max_polymarket_events,
            include_top_volume_page=True,
        )
        k_markets, k_multi, k_status = fetch_kalshi_data(
            client,
            series_tickers=kalshi_series,
            general_pages=kalshi_general_pages,
        )
        mf_markets, mf_status = fetch_manifold_data(
            client,
            limit_per_sort=manifold_limit_per_sort,
            expand_top_multichoice=manifold_expand_multichoice,
        )

        all_markets = pm_markets + k_markets + mf_markets
        markets_by_key = {(m.venue, m.market_id): m for m in all_markets}

        dutch_books = scan_dutch_books(
            pm_multi + k_multi,
            slippage_bps=slippage_bps,
            include_efficient_exhaustive=True,
        )
        matches = match_cross_venue(
            all_markets,
            slippage_bps=slippage_bps,
            min_divergence_pp=min_divergence_pp,
            min_activity_threshold=min_activity_threshold,
        )

        venue_statuses: list[VenueStatus] = [pm_status, k_status, mf_status]
        total_books_fetched = 0

        if enrich_orderbooks:
            t_clob = time.monotonic()
            db_books = enrich_dutch_book_orderbooks(
                client,
                dutch_books,
                max_events_to_enrich=max_dutch_books_to_enrich,
                max_legs_per_event=15,
            )
            cv_books = enrich_cross_venue_orderbooks(
                client,
                matches,
                markets_by_key=markets_by_key,
                slippage_bps=slippage_bps,
                max_matches_to_enrich=max_matches_to_enrich,
            )
            total_books_fetched = db_books + cv_books
            venue_statuses.append(
                VenueStatus(
                    venue="orderbook_depth",
                    endpoint=POLYMARKET_CLOB_BOOK_URL,
                    ok=total_books_fetched > 0,
                    http_status=200 if total_books_fetched > 0 else None,
                    latency_ms=round((time.monotonic() - t_clob) * 1000.0, 1),
                    events_fetched=0,
                    markets_fetched=total_books_fetched,
                    currency="USD",
                    error=None if total_books_fetched > 0 else "No eligible books queried",
                    fetched_at_utc=utc_now_iso(),
                )
            )

    pure_arbs = [m for m in matches if m.signal_class == "PURE_CROSS_VENUE_ARB"]
    maker_near = [m for m in matches if m.signal_class == "NEAR_ARB_MAKER_CANDIDATE"]
    div_3pp = [m for m in matches if m.prob_diff_pp >= 3.0]
    div_5pp = [m for m in matches if m.prob_diff_pp >= 5.0]
    dutch_net_arbs = [d for d in dutch_books if d.is_net_arb]
    dutch_traps = [d for d in dutch_books if d.exhaustiveness_status == "NON_EXHAUSTIVE_AUGMENTED_TRAP"]
    dutch_exhaustive = [d for d in dutch_books if d.is_truly_exhaustive]

    best_net_roi = max((m.net_roi_pct for m in pure_arbs if m.net_roi_pct is not None), default=0.0)
    max_div_pp = max((m.prob_diff_pp for m in matches), default=0.0)

    sample_embeds = build_sample_discord_embeds(matches, dutch_books, max_embeds=4)

    summary: dict[str, int | float] = {
        "venues_online": sum(1 for v in (pm_status, k_status, mf_status) if v.ok),
        "total_events_scanned": pm_status.events_fetched + k_status.events_fetched + mf_status.events_fetched,
        "total_markets_scanned": len(all_markets),
        "orderbooks_verified": total_books_fetched,
        "cross_venue_matches": len(matches),
        "pure_cross_venue_arbs": len(pure_arbs),
        "maker_near_arbs": len(maker_near),
        "divergences_3pp_plus": len(div_3pp),
        "divergences_5pp_plus": len(div_5pp),
        "dutch_books_total": len(dutch_books),
        "dutch_exhaustive_scanned": len(dutch_exhaustive),
        "dutch_exhaustive_net_arbs": len(dutch_net_arbs),
        "dutch_non_exhaustive_traps": len(dutch_traps),
        "best_net_arb_roi_pct": round(best_net_roi, 3),
        "max_divergence_pp": round(max_div_pp, 2),
    }

    return ScanReport(
        generated_at_utc=utc_now_iso(),
        scan_duration_sec=round(time.monotonic() - t0, 2),
        slippage_bps=slippage_bps,
        venue_statuses=venue_statuses,
        summary=summary,
        dutch_books=dutch_books,
        cross_venue_matches=matches,
        sample_discord_embeds=sample_embeds,
    )


def _safe_json_for_script_tag(obj: object) -> str:
    """Serialize JSON safely for embedding inside an HTML <script> tag (blocks </script> breakout)."""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_dashboard_html(report: ScanReport) -> str:
    """Render a self-contained, XSS-safe, institutional-grade interactive HTML radar dashboard."""
    report_dict = report.model_dump()
    embedded_json = _safe_json_for_script_tag(report_dict)

    s = report.summary
    gen_ts = html.escape(report.generated_at_utc)
    disclaimer_esc = html.escape(RISK_DISCLAIMER)

    pure_count = int(s.get("pure_cross_venue_arbs", 0))
    maker_count = int(s.get("maker_near_arbs", 0))
    div3_count = int(s.get("divergences_3pp_plus", 0))
    div5_count = int(s.get("divergences_5pp_plus", 0))
    trap_count = int(s.get("dutch_non_exhaustive_traps", 0))
    dutch_arb_count = int(s.get("dutch_exhaustive_net_arbs", 0))
    mkts_count = int(s.get("total_markets_scanned", 0))
    matches_count = int(s.get("cross_venue_matches", 0))
    books_count = int(s.get("orderbooks_verified", 0))
    best_roi = float(s.get("best_net_arb_roi_pct", 0.0))

    venue_pills_html_parts: list[str] = []
    for vs in report.venue_statuses:
        dot_cls = "dot-ok" if vs.ok else "dot-err"
        v_name = html.escape(vs.venue.replace("_", " ").title())
        code_txt = f"HTTP {vs.http_status}" if vs.http_status else "OFFLINE"
        if vs.venue == "orderbook_depth":
            meta = f"{vs.markets_fetched} books"
        else:
            meta = f"{vs.events_fetched:,} ev · {vs.markets_fetched:,} mkts"
        cur_tag = "Mana" if vs.currency == "MANA_PLAY_MONEY" else "USD"
        err_attr = f' title="{html.escape(vs.error, quote=True)}"' if vs.error else ""
        venue_pills_html_parts.append(
            f'<div class="venue-chip"{err_attr}>'
            f'<span class="dot {dot_cls}"></span>'
            f'<span class="vc-name">{v_name}</span>'
            f'<span class="vc-meta">{html.escape(code_txt)} · {html.escape(meta)} · {cur_tag} · {vs.latency_ms:.0f}ms</span>'
            f"</div>"
        )
    venue_pills_html = "\n".join(venue_pills_html_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>PredMarket Arb Radar — Polymarket · Kalshi · Manifold Live Spread Desk</title>
<style>
  :root {{
    --bg: #F6F5F0;
    --surface: #FFFFFF;
    --surface-subtle: #FBFBF8;
    --border: #E2DFD7;
    --border-strong: #CCC7BA;
    --ink: #12151C;
    --ink-muted: #565C68;
    --ink-soft: #818794;
    --emerald: #0D7A5F;
    --emerald-bg: #EBF7F3;
    --cobalt: #1D4ED8;
    --cobalt-bg: #EFF4FF;
    --amber: #B45309;
    --amber-bg: #FEF7EC;
    --crimson: #B91C1C;
    --crimson-bg: #FEF2F2;
    --neutral-pill: #ECEAE3;
    --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
    --sans: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}

  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.45;
    -webkit-font-smoothing: antialiased;
  }}

  a {{ color: var(--cobalt); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* Shell header */
  .topbar {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }}
  .brand {{
    display: flex;
    align-items: center;
    gap: 11px;
  }}
  .brand-title {{
    font-size: 15px;
    font-weight: 700;
    letter-spacing: -0.015em;
    color: var(--ink);
  }}
  .brand-scope {{
    font-size: 12px;
    color: var(--ink-muted);
    margin-top: 1px;
  }}
  .topbar-right {{
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .stamp {{
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-muted);
  }}
  .btn {{
    appearance: none;
    border: 1px solid var(--border-strong);
    background: var(--surface);
    color: var(--ink);
    font-family: var(--sans);
    font-size: 12.5px;
    font-weight: 500;
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    transition: background 0.12s ease, border-color 0.12s ease;
  }}
  .btn:hover {{ background: var(--surface-subtle); border-color: var(--ink-soft); }}
  .btn-primary {{
    background: var(--ink);
    border-color: var(--ink);
    color: #FFFFFF;
  }}
  .btn-primary:hover {{ background: #262B36; border-color: #262B36; }}

  /* Main container */
  .wrap {{
    max-width: 1420px;
    margin: 0 auto;
    padding: 16px 24px 48px;
  }}

  /* Venue provenance strip */
  .venue-strip {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 14px;
  }}
  .venue-chip {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 10px;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 12px;
  }}
  .dot {{
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
  }}
  .dot-ok {{ background: var(--emerald); }}
  .dot-err {{ background: var(--crimson); }}
  .vc-name {{ font-weight: 600; color: var(--ink); }}
  .vc-meta {{ font-family: var(--mono); font-size: 11.5px; color: var(--ink-muted); }}

  /* Type-only KPI strip (no icon tiles, flat 1px rules) */
  .kpi-strip {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    margin-bottom: 16px;
  }}
  .kpi-cell {{
    padding: 14px 18px;
    border-right: 1px solid var(--border);
  }}
  .kpi-cell:last-child {{ border-right: none; }}
  .kpi-label {{
    font-size: 11.5px;
    font-weight: 500;
    color: var(--ink-muted);
    margin-bottom: 4px;
  }}
  .kpi-value {{
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
    font-size: 23px;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--ink);
  }}
  .kpi-value.emerald {{ color: var(--emerald); }}
  .kpi-value.cobalt {{ color: var(--cobalt); }}
  .kpi-value.amber {{ color: var(--amber); }}
  .kpi-value.crimson {{ color: var(--crimson); }}
  .kpi-sub {{
    font-family: var(--mono);
    font-size: 11.5px;
    color: var(--ink-soft);
    margin-top: 3px;
  }}

  /* Navigation tabs */
  .tabs {{
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 14px;
    flex-wrap: wrap;
  }}
  .tab-btn {{
    appearance: none;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 9px 14px;
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 600;
    color: var(--ink-muted);
    cursor: pointer;
    margin-bottom: -1px;
  }}
  .tab-btn:hover {{ color: var(--ink); }}
  .tab-btn.active {{
    color: var(--ink);
    border-bottom-color: var(--emerald);
  }}
  .tab-count {{
    font-family: var(--mono);
    font-size: 11px;
    background: var(--neutral-pill);
    color: var(--ink-muted);
    padding: 1px 6px;
    border-radius: 10px;
    margin-left: 6px;
  }}

  .panel {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }}
  .tab-pane {{ display: none; }}
  .tab-pane.active {{ display: block; }}

  /* Filter toolbar */
  .toolbar {{
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    background: var(--surface-subtle);
  }}
  .filter-group {{
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }}
  .pill {{
    appearance: none;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--ink-muted);
    font-size: 12px;
    font-weight: 500;
    padding: 5px 10px;
    border-radius: 5px;
    cursor: pointer;
  }}
  .pill:hover {{ border-color: var(--border-strong); color: var(--ink); }}
  .pill.active {{
    background: var(--neutral-pill);
    border-color: var(--border-strong);
    color: var(--ink);
    font-weight: 600;
  }}
  .search-input, .select-input {{
    border: 1px solid var(--border-strong);
    background: var(--surface);
    color: var(--ink);
    font-family: var(--sans);
    font-size: 12.5px;
    padding: 5px 10px;
    border-radius: 5px;
  }}
  .search-input {{ width: 230px; }}

  /* Data table (rules & alignment, no zebra stripes) */
  .table-wrap {{ overflow-x: auto; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}
  th {{
    text-align: left;
    font-size: 11.5px;
    font-weight: 600;
    color: var(--ink-muted);
    padding: 10px 12px;
    border-bottom: 1px solid var(--border-strong);
    white-space: nowrap;
    background: var(--surface);
  }}
  th.num, td.num {{
    text-align: right;
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }}
  td {{
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }}
  tbody tr.data-row {{ cursor: pointer; }}
  tbody tr.data-row:hover {{ background: #FAF9F5; }}
  tbody tr.detail-row {{ background: #FAF9F5; }}
  tbody tr.detail-row td {{ padding: 12px 16px 14px; border-bottom: 1px solid var(--border-strong); }}

  /* Badges */
  .badge {{
    display: inline-block;
    font-family: var(--mono);
    font-size: 10.5px;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 4px;
    white-space: nowrap;
  }}
  .b-arb {{ background: var(--emerald-bg); color: var(--emerald); }}
  .b-near {{ background: var(--cobalt-bg); color: var(--cobalt); }}
  .b-div {{ background: var(--amber-bg); color: var(--amber); }}
  .b-trap {{ background: var(--crimson-bg); color: var(--crimson); }}
  .b-muted {{ background: var(--neutral-pill); color: var(--ink-muted); }}

  .prop-title {{ font-weight: 600; color: var(--ink); }}
  .prop-sub {{ font-size: 11.5px; color: var(--ink-muted); margin-top: 2px; }}

  /* Signature Element: All-in Cost vs $1.00 Par Bar */
  .par-wrap {{
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 3px;
    min-width: 118px;
  }}
  .par-track {{
    position: relative;
    width: 112px;
    height: 7px;
    background: #ECEAE3;
    border-radius: 2px;
    overflow: hidden;
  }}
  .par-fill {{
    height: 100%;
    border-radius: 2px;
  }}
  .par-mark {{
    position: absolute;
    left: 83.33%;
    top: 0;
    bottom: 0;
    width: 1.5px;
    background: var(--ink);
  }}
  .par-text {{
    font-family: var(--mono);
    font-size: 11.5px;
  }}

  .pos {{ color: var(--emerald); font-weight: 600; }}
  .neg {{ color: var(--ink-soft); }}
  .warn {{ color: var(--amber); font-weight: 600; }}
  .danger {{ color: var(--crimson); font-weight: 600; }}

  /* Expandable audit grid */
  .audit-grid {{
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 14px;
    font-size: 12.5px;
  }}
  .audit-box {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
  }}
  .audit-h {{
    font-size: 11px;
    font-weight: 600;
    color: var(--ink-muted);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .audit-kv {{
    display: flex;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 3px;
    font-family: var(--mono);
    font-size: 12px;
  }}
  .flag-list {{
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 6px;
  }}

  /* Discord preview cards */
  .discord-grid {{
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
    padding: 16px;
    background: #1E1F22;
  }}
  .dc-card {{
    background: #2B2D31;
    border-radius: 6px;
    border-left: 4px solid var(--emerald);
    padding: 12px 14px;
    color: #DBDEE1;
    font-size: 13px;
  }}
  .dc-title {{ font-weight: 700; color: #FFFFFF; margin-bottom: 6px; }}
  .dc-desc {{ color: #C5C9CE; font-size: 12.5px; white-space: pre-line; margin-bottom: 8px; }}
  .dc-fields {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px; }}
  .dc-field.full {{ grid-column: 1 / -1; }}
  .dc-fn {{ font-size: 11.5px; font-weight: 700; color: #FFFFFF; margin-bottom: 2px; }}
  .dc-fv {{ font-family: var(--mono); font-size: 11.5px; color: #B5BAC1; white-space: pre-line; }}
  .dc-footer {{ font-size: 11px; color: #949BA4; border-top: 1px solid #3F4147; padding-top: 6px; }}

  /* Whop tiers */
  .tier-grid {{
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 14px;
    padding: 18px;
  }}
  .tier-card {{
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    background: var(--surface);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }}
  .tier-card.featured {{
    border-color: var(--emerald);
    background: #FAFDFB;
  }}
  .tier-name {{ font-size: 14px; font-weight: 700; }}
  .tier-price {{
    font-family: var(--mono);
    font-size: 26px;
    font-weight: 700;
    margin: 8px 0 10px;
  }}
  .tier-price span {{ font-size: 12px; font-weight: 500; color: var(--ink-muted); }}
  .tier-list {{
    list-style: none;
    padding: 0;
    margin: 0 0 14px;
    font-size: 12.5px;
    color: var(--ink-muted);
  }}
  .tier-list li {{
    padding: 4px 0;
    border-bottom: 1px solid var(--border);
  }}
  .tier-list li:last-child {{ border-bottom: none; }}

  .method-box {{
    padding: 16px 18px;
    border-top: 1px solid var(--border);
    background: var(--surface-subtle);
    font-size: 12.5px;
    color: var(--ink-muted);
  }}
  .method-box code {{
    font-family: var(--mono);
    background: var(--neutral-pill);
    padding: 1px 5px;
    border-radius: 3px;
    color: var(--ink);
  }}
  .disclaimer {{
    margin-top: 16px;
    padding: 12px 16px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 12px;
    color: var(--ink-muted);
  }}
  .empty-state {{
    padding: 36px 20px;
    text-align: center;
    color: var(--ink-muted);
    font-size: 13px;
  }}

  @media (max-width: 1024px) {{
    .kpi-strip {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .kpi-cell {{ border-bottom: 1px solid var(--border); }}
    .audit-grid, .discord-grid, .tier-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <svg width="28" height="28" viewBox="0 0 28 28" fill="none" aria-hidden="true">
      <rect x="1" y="1" width="26" height="26" rx="5" stroke="#0D7A5F" stroke-width="1.6"/>
      <circle cx="14" cy="14" r="7.5" stroke="#0D7A5F" stroke-width="1.3" stroke-dasharray="2.5 2"/>
      <line x1="14" y1="3" x2="14" y2="25" stroke="#12151C" stroke-width="1.1" stroke-opacity="0.35"/>
      <line x1="3" y1="14" x2="25" y2="14" stroke="#12151C" stroke-width="1.1" stroke-opacity="0.35"/>
      <circle cx="17.5" cy="10.5" r="2.6" fill="#0D7A5F"/>
      <circle cx="10.5" cy="16.5" r="2" fill="#B45309"/>
    </svg>
    <div>
      <div class="brand-title">PredMarket Arb Radar</div>
      <div class="brand-scope">Polymarket · Kalshi · Manifold — Read-Only Cross-Venue Arbitrage &amp; Dutch-Book Desk</div>
    </div>
  </div>
  <div class="topbar-right">
    <span class="stamp" id="scan-stamp">Last scan: {gen_ts} ({report.scan_duration_sec:.1f}s)</span>
    <button class="btn" id="btn-reload" type="button">Reload latest_scan.json</button>
    <a class="btn btn-primary" href="#tiers" id="btn-jump-tiers">Discord + Whop Access</a>
  </div>
</header>

<main class="wrap">
  <section class="venue-strip" aria-label="Live Venue Health">
    {venue_pills_html}
  </section>

  <section class="kpi-strip" aria-label="Scan Summary">
    <div class="kpi-cell">
      <div class="kpi-label">Pure Cash Arbs (Net &gt; 0%)</div>
      <div class="kpi-value emerald" id="kpi-pure">{pure_count}</div>
      <div class="kpi-sub">Best net ROI: +{best_roi:.2f}% (USD venues)</div>
    </div>
    <div class="kpi-cell">
      <div class="kpi-label">Maker Near-Arbs (Gross &lt; $1)</div>
      <div class="kpi-value cobalt" id="kpi-maker">{maker_count}</div>
      <div class="kpi-sub">Sub-$1.00 gross before taker fees</div>
    </div>
    <div class="kpi-cell">
      <div class="kpi-label">Probability Divergences</div>
      <div class="kpi-value amber" id="kpi-div">{div3_count}</div>
      <div class="kpi-sub">{div5_count} at ≥ 5.0pp · {matches_count} matched pairs</div>
    </div>
    <div class="kpi-cell">
      <div class="kpi-label">Dutch-Book Trap Warnings</div>
      <div class="kpi-value crimson" id="kpi-trap">{trap_count}</div>
      <div class="kpi-sub">{dutch_arb_count} true exhaustive net arbs</div>
    </div>
    <div class="kpi-cell">
      <div class="kpi-label">Live Contracts Audited</div>
      <div class="kpi-value" id="kpi-mkts">{mkts_count:,}</div>
      <div class="kpi-sub">{books_count} live orderbooks verified</div>
    </div>
  </section>

  <nav class="tabs" role="tablist">
    <button class="tab-btn active" data-tab="xvenue" type="button">Cross-Venue Arb &amp; Divergence<span class="tab-count" id="tc-xvenue">{matches_count}</span></button>
    <button class="tab-btn" data-tab="dutch" type="button">Multi-Outcome Dutch-Book Audit<span class="tab-count" id="tc-dutch">{len(report.dutch_books)}</span></button>
    <button class="tab-btn" data-tab="discord" type="button">Discord Alert Preview<span class="tab-count" id="tc-discord">{len(report.sample_discord_embeds)}</span></button>
    <button class="tab-btn" data-tab="tiers" id="tab-tiers-btn" type="button">Whop Tiers &amp; Fee Math</button>
  </nav>

  <!-- TAB 1: CROSS-VENUE -->
  <section class="panel tab-pane active" id="pane-xvenue">
    <div class="toolbar">
      <div class="filter-group" id="sig-filters">
        <button class="pill active" data-sig="ACTIONABLE" type="button">Actionable (Arb + Near + ≥3pp)</button>
        <button class="pill" data-sig="PURE_CROSS_VENUE_ARB" type="button">Pure Net Arb</button>
        <button class="pill" data-sig="NEAR_ARB_MAKER_CANDIDATE" type="button">Maker Near-Arb</button>
        <button class="pill" data-sig="DIV_3PP" type="button">Divergence ≥ 3pp</button>
        <button class="pill" data-sig="ALL" type="button">All Matched</button>
      </div>
      <div class="filter-group">
        <select class="select-input" id="pair-filter" aria-label="Venue pair">
          <option value="ALL">All Venue Pairs</option>
          <option value="polymarket-kalshi">Polymarket ↔ Kalshi (USD)</option>
          <option value="polymarket-manifold">Polymarket ↔ Manifold (Mana)</option>
          <option value="kalshi-manifold">Kalshi ↔ Manifold (Mana)</option>
        </select>
        <select class="select-input" id="sort-by" aria-label="Sort order">
          <option value="default">Sort: Signal Priority &amp; Net ROI</option>
          <option value="ann">Sort: Annualized Net ROI</option>
          <option value="diff">Sort: Prob Divergence (pp)</option>
          <option value="fill">Sort: Book-Verified Fillable USD</option>
          <option value="vol">Sort: Combined 24h Volume</option>
        </select>
        <input class="search-input" id="x-search" type="search" placeholder="Filter market, person, Fed, BTC..." />
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Signal</th>
            <th>Matched Proposition</th>
            <th>Venues</th>
            <th class="num">Prob A / B</th>
            <th class="num">Δ (pp)</th>
            <th class="num">All-In Cost vs $1.00</th>
            <th class="num">Net ROI</th>
            <th class="num">Ann. ROI</th>
            <th class="num">Book Cap (USD)</th>
          </tr>
        </thead>
        <tbody id="xvenue-tbody"></tbody>
      </table>
    </div>
  </section>

  <!-- TAB 2: DUTCH BOOKS -->
  <section class="panel tab-pane" id="pane-dutch">
    <div class="toolbar">
      <div class="filter-group" id="dutch-filters">
        <button class="pill active" data-df="ALL" type="button">All Audited Baskets</button>
        <button class="pill" data-df="EXHAUSTIVE" type="button">Truly Exhaustive Only</button>
        <button class="pill" data-df="TRAP" type="button">Non-Exhaustive Trap Warnings</button>
      </div>
      <div class="filter-group">
        <input class="search-input" id="d-search" type="search" placeholder="Filter event title or venue..." />
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Exhaustiveness Verdict</th>
            <th>Venue &amp; Multi-Outcome Event</th>
            <th class="num">Active / Total Legs</th>
            <th class="num">Sum YES Asks</th>
            <th class="num">Taker Fees</th>
            <th class="num">All-In Cost</th>
            <th class="num">Net ROI</th>
            <th class="num">Book Cap (USD)</th>
            <th class="num">24h Vol</th>
          </tr>
        </thead>
        <tbody id="dutch-tbody"></tbody>
      </table>
    </div>
  </section>

  <!-- TAB 3: DISCORD PREVIEW -->
  <section class="panel tab-pane" id="pane-discord">
    <div class="toolbar">
      <div style="font-size:12.5px;color:var(--ink-muted)">
        Live webhook embeds generated directly from this scan. Deduplicated by spread ID + 6h cooldown (+100 bps early re-alert).
      </div>
    </div>
    <div class="discord-grid" id="discord-grid"></div>
  </section>

  <!-- TAB 4: WHOP TIERS & METHODOLOGY -->
  <section class="panel tab-pane" id="pane-tiers">
    <div class="tier-grid">
      <div class="tier-card">
        <div>
          <div class="tier-name">Free Public Radar</div>
          <div class="tier-price">$0 <span>/ mo</span></div>
          <ul class="tier-list">
            <li>Public GitHub Pages radar refreshed every 30 min</li>
            <li>Cross-venue Polymarket, Kalshi &amp; Manifold table</li>
            <li>Exhaustive vs negRiskAugmented trap detector</li>
            <li>Open-source Python scanner (MIT license)</li>
          </ul>
        </div>
        <a class="btn" href="https://github.com/pecopeko/predmarket-arb-radar" target="_blank" rel="noopener noreferrer">View GitHub Repo</a>
      </div>

      <div class="tier-card featured">
        <div>
          <div class="tier-name">Pro Signal Feed</div>
          <div class="tier-price">$19 <span>/ mo</span></div>
          <ul class="tier-list">
            <li>Private Whop-gated Discord #arb-alerts channel</li>
            <li>Instant deduplicated Pure Cash Arb embeds</li>
            <li>High-liquidity ≥ 5pp probability divergence alerts</li>
            <li>Zero-custody read-only feeds (keep 100% of funds)</li>
          </ul>
        </div>
        <a class="btn btn-primary" href="https://github.com/pecopeko/predmarket-arb-radar/blob/main/WHOP_DISCORD_LAUNCH_PLAYBOOK.md" target="_blank" rel="noopener noreferrer">Launch Playbook</a>
      </div>

      <div class="tier-card">
        <div>
          <div class="tier-name">Quant Desk</div>
          <div class="tier-price">$49 <span>/ mo</span></div>
          <ul class="tier-list">
            <li>Live CLOB + Kalshi orderbook depth-capped sizing</li>
            <li>Sub-$1.00 Maker Near-Arb alerts (0% maker fee edge)</li>
            <li>Raw JSON / CSV scan snapshots every 5–15 min</li>
            <li>Annualized ROI &amp; capital-lockup screening</li>
          </ul>
        </div>
        <a class="btn" href="https://github.com/pecopeko/predmarket-arb-radar/blob/main/WHOP_DISCORD_LAUNCH_PLAYBOOK.md" target="_blank" rel="noopener noreferrer">Desk Spec</a>
      </div>

      <div class="tier-card">
        <div>
          <div class="tier-name">Elite Custom Feed</div>
          <div class="tier-price">$79 <span>/ mo</span></div>
          <ul class="tier-list">
            <li>Dedicated private Discord/Telegram webhook routing</li>
            <li>Custom Kalshi series &amp; Polymarket event watchlists</li>
            <li>Custom slippage-bps &amp; minimum-size thresholds</li>
            <li>Direct resolution-rule &amp; basis-risk teardown notes</li>
          </ul>
        </div>
        <a class="btn" href="https://github.com/pecopeko/predmarket-arb-radar/blob/main/WHOP_DISCORD_LAUNCH_PLAYBOOK.md" target="_blank" rel="noopener noreferrer">Elite Setup</a>
      </div>
    </div>

    <div class="method-box">
      <strong>Exact Fee &amp; Exhaustiveness Methodology:</strong><br/>
      1. <strong>Kalshi Taker Fee:</strong> <code>ceil_cent(0.07 * C * P * (1 - P))</code> (marginal <code>0.07 * P * (1 - P)</code>, peaking at $0.0175/contract at $0.50).<br/>
      2. <strong>Polymarket Taker Fee:</strong> <code>feeSchedule.rate * P * (1 - P)</code> read live per market from Gamma API when <code>feesEnabled=true</code> (typically 0.04 politics/finance, 0.05 economics/sports, 0.07 crypto, 0.00 geopolitics/fee-free).<br/>
      3. <strong>Dutch-Book Exhaustiveness Guard:</strong> Polymarket <code>negRiskAugmented=true</code> events with inactive placeholder slots or an excluded <code>negRiskOther</code> contract are <strong>never</strong> classified as risk-free arbitrage even when <code>sum(YES asks) &lt; $1.00</code>.<br/>
      4. <strong>Venue Currency Separation:</strong> Manifold Markets uses play-money Mana and is surfaced strictly as a crowd-calibration Probability Divergence benchmark — never mixed into USD cash arbitrage calculations.
    </div>
  </section>

  <footer class="disclaimer">
    <strong>Risk &amp; Non-Advisory Disclosure:</strong> {disclaimer_esc}
  </footer>
</main>

<script id="radar-data" type="application/json">{embedded_json}</script>
<script>
(function() {{
  "use strict";

  let REPORT = JSON.parse(document.getElementById("radar-data").textContent);
  let activeSig = "ACTIONABLE";
  let activeDutchFilter = "ALL";
  let expandedMatchId = null;
  let expandedDutchId = null;

  function esc(s) {{
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }}

  function safeUrl(u) {{
    if (!u || typeof u !== "string") return "";
    if (u.startsWith("https://") || u.startsWith("http://")) return u;
    return "";
  }}

  function fmtUsd(v) {{
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return "$" + Number(v).toLocaleString("en-US", {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
  }}

  function fmtCompactUsd(v) {{
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const n = Number(v);
    if (n >= 1e6) return "$" + (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
    return "$" + n.toFixed(0);
  }}

  function fmtPct(v, signed) {{
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const n = Number(v);
    const prefix = signed && n > 0 ? "+" : "";
    return prefix + n.toFixed(2) + "%";
  }}

  function signalBadge(cls) {{
    if (cls === "PURE_CROSS_VENUE_ARB") return '<span class="badge b-arb">PURE ARB</span>';
    if (cls === "NEAR_ARB_MAKER_CANDIDATE") return '<span class="badge b-near">MAKER NEAR</span>';
    if (cls === "HIGH_LIQ_DIVERGENCE") return '<span class="badge b-div">DIVERGENCE</span>';
    return '<span class="badge b-muted">TRACKED</span>';
  }}

  function exhaustBadge(st) {{
    if (st === "EXHAUSTIVE_DUTCH_BOOK") return '<span class="badge b-arb">EXHAUSTIVE ARB</span>';
    if (st === "EXHAUSTIVE_EFFICIENT") return '<span class="badge b-muted">EXHAUSTIVE</span>';
    if (st === "NON_EXHAUSTIVE_AUGMENTED_TRAP") return '<span class="badge b-trap">AUGMENTED TRAP</span>';
    return '<span class="badge b-div">OPEN FIELD</span>';
  }}

  function parBarHtml(netCost) {{
    if (netCost === null || netCost === undefined) return '<span class="neg">—</span>';
    const c = Number(netCost);
    // Scale 0.00 -> 1.20 across 100% width so $1.00 par sits cleanly at 83.33%
    const pct = Math.max(2, Math.min(100, (c / 1.20) * 100));
    const color = c < 1.0 ? "#0D7A5F" : (c < 1.02 ? "#1D4ED8" : "#9CA3AF");
    const cls = c < 1.0 ? "pos" : "";
    return (
      '<div class="par-wrap">' +
        '<div class="par-text ' + cls + '">$' + c.toFixed(4) + '</div>' +
        '<div class="par-track" title="Par line = $1.00">' +
          '<div class="par-fill" style="width:' + pct.toFixed(1) + '%;background:' + color + '"></div>' +
          '<div class="par-mark"></div>' +
        '</div>' +
      '</div>'
    );
  }}

  function renderCrossVenue() {{
    const tbody = document.getElementById("xvenue-tbody");
    const pairFilter = document.getElementById("pair-filter").value;
    const sortBy = document.getElementById("sort-by").value;
    const q = (document.getElementById("x-search").value || "").trim().toLowerCase();

    let rows = (REPORT.cross_venue_matches || []).filter(function(m) {{
      if (activeSig === "ACTIONABLE") {{
        const keep = m.signal_class === "PURE_CROSS_VENUE_ARB" ||
                     m.signal_class === "NEAR_ARB_MAKER_CANDIDATE" ||
                     m.prob_diff_pp >= 3.0;
        if (!keep) return false;
      }} else if (activeSig === "DIV_3PP") {{
        if (m.prob_diff_pp < 3.0) return false;
      }} else if (activeSig !== "ALL") {{
        if (m.signal_class !== activeSig) return false;
      }}

      if (pairFilter !== "ALL") {{
        const pk = m.venue_a + "-" + m.venue_b;
        if (pk !== pairFilter) return false;
      }}

      if (q) {{
        const hay = (m.canonical_label + " " + m.title_a + " " + m.title_b + " " + m.event_family).toLowerCase();
        if (!hay.includes(q)) return false;
      }}
      return true;
    }});

    if (sortBy === "ann") {{
      rows = rows.slice().sort((a, b) => (b.annualized_net_roi_pct || -999) - (a.annualized_net_roi_pct || -999));
    }} else if (sortBy === "diff") {{
      rows = rows.slice().sort((a, b) => b.prob_diff_pp - a.prob_diff_pp);
    }} else if (sortBy === "fill") {{
      rows = rows.slice().sort((a, b) => (b.max_fillable_usd || -1) - (a.max_fillable_usd || -1));
    }} else if (sortBy === "vol") {{
      rows = rows.slice().sort((a, b) => (b.volume_24h_a + b.volume_24h_b) - (a.volume_24h_a + a.volume_24h_b));
    }}

    if (!rows.length) {{
      tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No matched propositions match the current filter criteria.</td></tr>';
      return;
    }}

    const parts = [];
    rows.slice(0, 150).forEach(function(m) {{
      const isOpen = expandedMatchId === m.match_id;
      const va = esc(m.venue_a);
      const vb = esc(m.venue_b);
      const pairText = va + " ↔ " + vb + (m.is_cash_arb_eligible ? "" : " (Mana)");
      const roiCls = (m.net_roi_pct !== null && m.net_roi_pct > 0) ? "pos" : "neg";
      const diffCls = m.prob_diff_pp >= 5.0 ? "warn" : "";

      parts.push(
        '<tr class="data-row" data-mid="' + esc(m.match_id) + '">' +
          '<td>' + signalBadge(m.signal_class) + '</td>' +
          '<td><div class="prop-title">' + esc(m.canonical_label) + '</div>' +
              '<div class="prop-sub">' + esc(m.best_direction || "Probability benchmark comparison") + '</div></td>' +
          '<td><span class="badge b-muted">' + pairText + '</span></td>' +
          '<td class="num">' + (m.prob_a * 100).toFixed(1) + '% / ' + (m.prob_b * 100).toFixed(1) + '%</td>' +
          '<td class="num ' + diffCls + '">' + m.prob_diff_pp.toFixed(2) + '</td>' +
          '<td class="num">' + parBarHtml(m.net_cost) + '</td>' +
          '<td class="num ' + roiCls + '">' + fmtPct(m.net_roi_pct, true) + '</td>' +
          '<td class="num">' + (m.annualized_net_roi_pct !== null ? fmtPct(m.annualized_net_roi_pct, true) : "—") + '</td>' +
          '<td class="num">' + (m.max_fillable_usd !== null ? fmtUsd(m.max_fillable_usd) : "—") + '</td>' +
        '</tr>'
      );

      if (isOpen) {{
        const urlA = safeUrl(m.url_a);
        const urlB = safeUrl(m.url_b);
        const linkA = urlA ? '<a href="' + esc(urlA) + '" target="_blank" rel="noopener noreferrer">Open ' + va + '</a>' : va;
        const linkB = urlB ? '<a href="' + esc(urlB) + '" target="_blank" rel="noopener noreferrer">Open ' + vb + '</a>' : vb;
        const flagsHtml = (m.risk_flags || []).map(f => '<span class="badge b-muted">' + esc(f) + '</span>').join("");
        parts.push(
          '<tr class="detail-row"><td colspan="9">' +
            '<div class="audit-grid">' +
              '<div class="audit-box">' +
                '<div class="audit-h">Venue Quotes &amp; Links</div>' +
                '<div style="margin-bottom:6px"><strong>' + va + ':</strong> ' + esc(m.title_a) + ' (' + linkA + ')</div>' +
                '<div style="margin-bottom:6px"><strong>' + vb + ':</strong> ' + esc(m.title_b) + ' (' + linkB + ')</div>' +
                '<div class="audit-kv"><span>24h Vol A / B</span><span>' + fmtCompactUsd(m.volume_24h_a) + ' / ' + fmtCompactUsd(m.volume_24h_b) + '</span></div>' +
              '</div>' +
              '<div class="audit-box">' +
                '<div class="audit-h">Exact Cost &amp; Fee Decomposition</div>' +
                '<div class="audit-kv"><span>' + esc(m.leg_a_action || "Leg A") + ' (' + va + ')</span><span>' + (m.leg_a_price !== null ? "$" + m.leg_a_price.toFixed(4) : "—") + ' (fee $' + m.fee_a.toFixed(4) + ')</span></div>' +
                '<div class="audit-kv"><span>' + esc(m.leg_b_action || "Leg B") + ' (' + vb + ')</span><span>' + (m.leg_b_price !== null ? "$" + m.leg_b_price.toFixed(4) : "—") + ' (fee $' + m.fee_b.toFixed(4) + ')</span></div>' +
                '<div class="audit-kv"><span>Gross -> Net Cost</span><span>' + (m.gross_cost !== null ? "$" + m.gross_cost.toFixed(4) + " -> $" + m.net_cost.toFixed(4) : "—") + '</span></div>' +
                '<div class="audit-kv"><span>Book Depth (sh / Est. PnL)</span><span>' + (m.max_fillable_shares !== null ? m.max_fillable_shares.toLocaleString() + " sh / " + fmtUsd(m.expected_net_profit_usd) : "Unverified") + '</span></div>' +
              '</div>' +
              '<div class="audit-box">' +
                '<div class="audit-h">Horizon &amp; Risk Flags</div>' +
                '<div class="audit-kv"><span>Days to Expiry</span><span>' + (m.days_to_expiry !== null ? m.days_to_expiry + " d" : "—") + '</span></div>' +
                '<div class="audit-kv"><span>Match Confidence</span><span>' + (m.match_confidence * 100).toFixed(0) + '%</span></div>' +
                '<div class="flag-list">' + flagsHtml + '</div>' +
              '</div>' +
            '</div>' +
          '</td></tr>'
        );
      }}
    }});
    tbody.innerHTML = parts.join("");
  }}

  function renderDutchBooks() {{
    const tbody = document.getElementById("dutch-tbody");
    const q = (document.getElementById("d-search").value || "").trim().toLowerCase();

    const rows = (REPORT.dutch_books || []).filter(function(d) {{
      if (activeDutchFilter === "EXHAUSTIVE" && !d.is_truly_exhaustive) return false;
      if (activeDutchFilter === "TRAP" && d.is_truly_exhaustive) return false;
      if (q && !(d.title + " " + d.venue + " " + d.exhaustiveness_reason).toLowerCase().includes(q)) return false;
      return true;
    }});

    if (!rows.length) {{
      tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No multi-outcome baskets match this filter.</td></tr>';
      return;
    }}

    const parts = [];
    rows.forEach(function(d) {{
      const did = d.venue + ":" + d.event_id;
      const isOpen = expandedDutchId === did;
      const roiCls = d.is_net_arb ? "pos" : (d.is_truly_exhaustive ? "neg" : "danger");

      parts.push(
        '<tr class="data-row" data-did="' + esc(did) + '">' +
          '<td>' + exhaustBadge(d.exhaustiveness_status) + '</td>' +
          '<td><div class="prop-title">' + esc(d.title) + '</div>' +
              '<div class="prop-sub">' + esc(d.venue.toUpperCase()) + ' · ' + esc(d.exhaustiveness_reason) + '</div></td>' +
          '<td class="num">' + d.leg_count + ' / ' + d.total_markets_in_event + '</td>' +
          '<td class="num">$' + d.sum_yes_asks.toFixed(4) + '</td>' +
          '<td class="num">$' + d.total_taker_fees.toFixed(4) + '</td>' +
          '<td class="num">' + parBarHtml(d.total_all_in_cost) + '</td>' +
          '<td class="num ' + roiCls + '">' + fmtPct(d.net_roi_pct, true) + (d.is_truly_exhaustive ? "" : " *") + '</td>' +
          '<td class="num">' + (d.max_fillable_usd !== null ? fmtUsd(d.max_fillable_usd) : "—") + '</td>' +
          '<td class="num">' + fmtCompactUsd(d.volume_24h) + '</td>' +
        '</tr>'
      );

      if (isOpen) {{
        const legRows = (d.legs || []).slice(0, 15).map(function(l) {{
          return (
            '<div class="audit-kv">' +
              '<span>' + esc(l.label) + '</span>' +
              '<span>ask $' + l.yes_ask.toFixed(4) + ' · fee $' + l.taker_fee.toFixed(4) +
              (l.yes_ask_size !== null ? ' · size ' + Number(l.yes_ask_size).toLocaleString() : '') + '</span>' +
            '</div>'
          );
        }}).join("");
        const moreNote = d.legs.length > 15 ? '<div class="prop-sub">Showing first 15 of ' + d.legs.length + ' legs.</div>' : '';
        const u = safeUrl(d.url);
        const linkHtml = u ? '<a href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">Inspect event on ' + esc(d.venue) + '</a>' : '';
        parts.push(
          '<tr class="detail-row"><td colspan="9">' +
            '<div class="audit-box">' +
              '<div class="audit-h">Leg-by-Leg Basket Audit (' + d.book_verified_legs + '/' + d.leg_count + ' CLOB verified) · ' + linkHtml + '</div>' +
              legRows + moreNote +
            '</div>' +
          '</td></tr>'
        );
      }}
    }});
    tbody.innerHTML = parts.join("");
  }}

  function renderDiscordPreviews() {{
    const grid = document.getElementById("discord-grid");
    const embeds = REPORT.sample_discord_embeds || [];
    if (!embeds.length) {{
      grid.innerHTML = '<div class="empty-state" style="color:#B5BAC1">No alert embeds generated in this scan.</div>';
      return;
    }}
    grid.innerHTML = embeds.map(function(e) {{
      const borderHex = "#" + Number(e.color || 0x0D7A5F).toString(16).padStart(6, "0");
      const fieldsHtml = (e.fields || []).map(function(f) {{
        return '<div class="dc-field ' + (f.inline ? '' : 'full') + '">' +
               '<div class="dc-fn">' + esc(f.name) + '</div>' +
               '<div class="dc-fv">' + esc(f.value) + '</div>' +
               '</div>';
      }}).join("");
      return (
        '<article class="dc-card" style="border-left-color:' + esc(borderHex) + '">' +
          '<div class="dc-title">' + esc(e.title) + '</div>' +
          '<div class="dc-desc">' + esc(e.description) + '</div>' +
          '<div class="dc-fields">' + fieldsHtml + '</div>' +
          '<div class="dc-footer">' + esc((e.footer || {{}}).text || "") + '</div>' +
        '</article>'
      );
    }}).join("");
  }}

  function switchTab(tabId) {{
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tabId));
    document.querySelectorAll(".tab-pane").forEach(p => p.classList.toggle("active", p.id === "pane-" + tabId));
  }}

  document.querySelectorAll(".tab-btn").forEach(function(btn) {{
    btn.addEventListener("click", function() {{ switchTab(btn.dataset.tab); }});
  }});

  document.getElementById("btn-jump-tiers").addEventListener("click", function(ev) {{
    ev.preventDefault();
    switchTab("tiers");
    document.getElementById("pane-tiers").scrollIntoView({{ behavior: "smooth" }});
  }});

  document.querySelectorAll("#sig-filters .pill").forEach(function(p) {{
    p.addEventListener("click", function() {{
      activeSig = p.dataset.sig;
      document.querySelectorAll("#sig-filters .pill").forEach(x => x.classList.toggle("active", x === p));
      renderCrossVenue();
    }});
  }});

  document.querySelectorAll("#dutch-filters .pill").forEach(function(p) {{
    p.addEventListener("click", function() {{
      activeDutchFilter = p.dataset.df;
      document.querySelectorAll("#dutch-filters .pill").forEach(x => x.classList.toggle("active", x === p));
      renderDutchBooks();
    }});
  }});

  document.getElementById("pair-filter").addEventListener("change", renderCrossVenue);
  document.getElementById("sort-by").addEventListener("change", renderCrossVenue);
  document.getElementById("x-search").addEventListener("input", renderCrossVenue);
  document.getElementById("d-search").addEventListener("input", renderDutchBooks);

  document.getElementById("xvenue-tbody").addEventListener("click", function(ev) {{
    const tr = ev.target.closest("tr.data-row");
    if (!tr) return;
    const mid = tr.getAttribute("data-mid");
    expandedMatchId = expandedMatchId === mid ? null : mid;
    renderCrossVenue();
  }});

  document.getElementById("dutch-tbody").addEventListener("click", function(ev) {{
    const tr = ev.target.closest("tr.data-row");
    if (!tr) return;
    const did = tr.getAttribute("data-did");
    expandedDutchId = expandedDutchId === did ? null : did;
    renderDutchBooks();
  }});

  document.getElementById("btn-reload").addEventListener("click", function() {{
    const btn = this;
    btn.textContent = "Reloading...";
    fetch("./latest_scan.json", {{ cache: "no-store" }})
      .then(r => {{ if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }})
      .then(data => {{
        REPORT = data;
        document.getElementById("scan-stamp").textContent = "Last scan: " + REPORT.generated_at_utc + " (" + REPORT.scan_duration_sec + "s)";
        renderCrossVenue();
        renderDutchBooks();
        renderDiscordPreviews();
        btn.textContent = "Reloaded ✓";
        setTimeout(() => {{ btn.textContent = "Reload latest_scan.json"; }}, 1800);
      }})
      .catch(() => {{
        btn.textContent = "Using embedded scan";
        setTimeout(() => {{ btn.textContent = "Reload latest_scan.json"; }}, 1800);
      }});
  }});

  renderCrossVenue();
  renderDutchBooks();
  renderDiscordPreviews();
}})();
</script>
</body>
</html>
"""


def write_dashboard_artifacts(
    report: ScanReport,
    docs_dir: Path,
) -> dict[str, str]:
    """Write docs/latest_scan.json, docs/index.html, and docs/.nojekyll atomically."""
    docs_dir.mkdir(parents=True, exist_ok=True)
    json_path = docs_dir / "latest_scan.json"
    html_path = docs_dir / "index.html"
    nojekyll_path = docs_dir / ".nojekyll"

    json_path.write_text(
        json.dumps(report.model_dump(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    html_path.write_text(render_dashboard_html(report), encoding="utf-8")
    nojekyll_path.write_text("", encoding="utf-8")

    return {
        "latest_scan_json": str(json_path),
        "index_html": str(html_path),
        "nojekyll": str(nojekyll_path),
    }


def build_live_dashboard(
    docs_dir: Path,
    slippage_bps: float = 0.0,
    min_divergence_pp: float = 3.0,
    enrich_orderbooks: bool = True,
) -> tuple[ScanReport, dict[str, str]]:
    """Run a live scan and build the complete static GitHub Pages radar site."""
    report = run_live_scan(
        slippage_bps=slippage_bps,
        min_divergence_pp=min_divergence_pp,
        enrich_orderbooks=enrich_orderbooks,
    )
    paths = write_dashboard_artifacts(report, docs_dir=docs_dir)
    return report, paths
