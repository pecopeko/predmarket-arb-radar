"""Comprehensive unit, security, and live zero-key smoke tests for Track 14: PredMarket Arb Radar."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

TRACK14_SRC = Path(__file__).resolve().parents[1] / "src"
if str(TRACK14_SRC) not in sys.path:
    sys.path.insert(0, str(TRACK14_SRC))

from predmarket_radar.alerter import (  # noqa: E402
    AlertDeduplicator,
    dispatch_alerts,
    format_cross_venue_discord_embed,
    format_cross_venue_telegram_html,
    format_dutch_book_discord_embed,
)
from predmarket_radar.arb_engine import (  # noqa: E402
    extract_proposition,
    kalshi_order_taker_fee,
    kalshi_taker_fee_per_contract,
    match_cross_venue,
    polymarket_taker_fee_per_share,
    propositions_compatible,
    scan_dutch_books,
)
from predmarket_radar.dashboard_builder import render_dashboard_html, run_live_scan  # noqa: E402
from predmarket_radar.models import (  # noqa: E402
    MultiOutcomeEvent,
    NormalizedMarket,
    ScanReport,
    VenueStatus,
)
from predmarket_radar.venues import (  # noqa: E402
    fetch_kalshi_data,
    parse_kalshi_orderbook,
    parse_polymarket_clob_book,
)


# ---------------------------------------------------------------------------
# 1. Exact Fee Math & Orderbook Parsing Tests
# ---------------------------------------------------------------------------


def test_kalshi_taker_fee_math_and_rounding() -> None:
    # Peak at P = 0.50 -> 0.07 * 0.5 * 0.5 = $0.0175/contract
    assert kalshi_taker_fee_per_contract(0.50) == pytest.approx(0.0175, abs=1e-6)
    # Symmetry: P = 0.10 and P = 0.90 both yield $0.0063/contract
    assert kalshi_taker_fee_per_contract(0.10) == pytest.approx(0.0063, abs=1e-6)
    assert kalshi_taker_fee_per_contract(0.90) == pytest.approx(0.0063, abs=1e-6)
    # Extremes yield $0.00
    assert kalshi_taker_fee_per_contract(0.0) == 0.0
    assert kalshi_taker_fee_per_contract(1.0) == 0.0
    # Order-level upward rounding to nearest cent
    assert kalshi_order_taker_fee(0.50, 100) == pytest.approx(1.75, abs=1e-6)
    assert kalshi_order_taker_fee(0.10, 100) == pytest.approx(0.63, abs=1e-6)
    assert kalshi_order_taker_fee(0.10, 10) == pytest.approx(0.07, abs=1e-6)  # ceil(0.063) -> 0.07
    assert kalshi_order_taker_fee(0.50, 0) == 0.0


def test_polymarket_taker_fee_math() -> None:
    # Fee-free geopolitics market
    assert polymarket_taker_fee_per_share(0.50, fee_rate=0.04, fees_enabled=False) == 0.0
    # Economics (rate=0.05) at P=0.50 -> $0.0125/share
    assert polymarket_taker_fee_per_share(0.50, fee_rate=0.05, fees_enabled=True) == pytest.approx(0.0125, abs=1e-6)
    # Politics (rate=0.04) at P=0.936 -> 0.04 * 0.936 * 0.064 = 0.002396
    assert polymarket_taker_fee_per_share(0.936, fee_rate=0.04, fees_enabled=True) == pytest.approx(0.002396, abs=1e-6)


def test_polymarket_clob_book_sorts_descending_asks_correctly() -> None:
    # Polymarket CLOB returns asks descending (0.999 first, 0.042 last) and bids ascending!
    payload = {
        "asset_id": "tok_yes_1",
        "bids": [
            {"price": "0.001", "size": "50000"},
            {"price": "0.040", "size": "1200.5"},
            {"price": "0.020", "size": "9000"},
        ],
        "asks": [
            {"price": "0.999", "size": "31000000"},
            {"price": "0.500", "size": "400"},
            {"price": "0.042", "size": "850.25"},
        ],
    }
    snap = parse_polymarket_clob_book(payload, market_id="m1", token_id="tok_yes_1")
    assert snap.best_ask == pytest.approx(0.042)
    assert snap.best_ask_size == pytest.approx(850.25)
    assert snap.best_bid == pytest.approx(0.040)
    assert snap.best_bid_size == pytest.approx(1200.5)
    assert [lvl.price for lvl in snap.asks] == [0.042, 0.5, 0.999]
    assert [lvl.price for lvl in snap.bids] == [0.04, 0.02, 0.001]


def test_kalshi_orderbook_complementary_ask_derivation() -> None:
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.0500", "100.00"], ["0.0800", "450.00"]],
            "no_dollars": [["0.9000", "250.00"], ["0.9100", "600.00"]],
        }
    }
    snap_yes = parse_kalshi_orderbook(payload, ticker="KXTEST", side="yes")
    # Best YES ask = 1.00 - max(no_bid=0.91) = 0.09 with size 600
    assert snap_yes.best_bid == pytest.approx(0.08)
    assert snap_yes.best_ask == pytest.approx(0.09)
    assert snap_yes.best_ask_size == pytest.approx(600.0)

    snap_no = parse_kalshi_orderbook(payload, ticker="KXTEST", side="no")
    # Best NO ask = 1.00 - max(yes_bid=0.08) = 0.92 with size 450
    assert snap_no.best_bid == pytest.approx(0.91)
    assert snap_no.best_ask == pytest.approx(0.92)
    assert snap_no.best_ask_size == pytest.approx(450.0)


# ---------------------------------------------------------------------------
# 2. Intra-Venue Multi-Outcome Dutch-Book & Exhaustiveness Guard Tests
# ---------------------------------------------------------------------------


def _mk_leg(
    venue: str,
    mid: str,
    label: str,
    ask: float | None,
    size: float | None = 500.0,
    fee_rate: float = 0.0,
    fees_enabled: bool = False,
) -> NormalizedMarket:
    return NormalizedMarket(
        venue=venue,
        currency="USD",
        market_id=mid,
        event_id="ev1",
        event_title="Test Event",
        question=f"Will {label} happen?",
        outcome_label=label,
        yes_ask=ask,
        yes_ask_size=size,
        mid_prob=ask,
        fees_enabled=fees_enabled,
        taker_fee_rate=fee_rate,
    )


def test_dutch_book_detects_true_exhaustive_arb_with_depth_cap() -> None:
    ev = MultiOutcomeEvent(
        venue="polymarket",
        currency="USD",
        event_id="pm-fed-dec",
        title="Fed Decision in December?",
        is_mutually_exclusive=True,
        neg_risk=True,
        neg_risk_augmented=False,
        total_markets_in_event=3,
        active_markets_count=3,
        inactive_placeholder_count=0,
        has_excluded_other_leg=False,
        volume_24h=2_000_000.0,
        legs=[
            _mk_leg("polymarket", "l1", "Cut", 0.20, size=1000.0, fee_rate=0.05, fees_enabled=True),
            _mk_leg("polymarket", "l2", "Hold", 0.50, size=250.0, fee_rate=0.05, fees_enabled=True),
            _mk_leg("polymarket", "l3", "Hike", 0.25, size=800.0, fee_rate=0.05, fees_enabled=True),
        ],
    )
    res = scan_dutch_books([ev])
    assert len(res) == 1
    opp = res[0]
    assert opp.is_truly_exhaustive is True
    assert opp.exhaustiveness_status == "EXHAUSTIVE_DUTCH_BOOK"
    assert opp.sum_yes_asks == pytest.approx(0.95, abs=1e-6)
    # Expected fees: 0.05*(0.2*0.8 + 0.5*0.5 + 0.25*0.75) = 0.05*(0.16 + 0.25 + 0.1875) = 0.029875
    assert opp.total_taker_fees == pytest.approx(0.029875, abs=1e-5)
    assert opp.total_all_in_cost == pytest.approx(0.979875, abs=1e-5)
    assert opp.is_net_arb is True
    assert opp.net_roi_pct == pytest.approx(((1.0 - 0.979875) / 0.979875) * 100.0, abs=1e-2)
    # Depth capped by bottleneck leg ("Hold", 250 shares)
    assert opp.max_fillable_shares == pytest.approx(250.0)
    assert opp.max_fillable_usd == pytest.approx(250.0 * 0.979875, abs=0.05)
    assert opp.expected_net_profit_usd == pytest.approx(250.0 * (1.0 - 0.979875), abs=0.05)


def test_dutch_book_flags_neg_risk_augmented_as_trap_not_net_arb() -> None:
    # Mirrors real Polymarket 2028 Dem Nominee trap: sum_asks=0.953, but negRiskAugmented=True & 70 placeholders!
    ev = MultiOutcomeEvent(
        venue="polymarket",
        currency="USD",
        event_id="pm-dem-2028",
        title="Democratic Presidential Nominee 2028",
        is_mutually_exclusive=True,
        neg_risk=True,
        neg_risk_augmented=True,
        total_markets_in_event=128,
        active_markets_count=2,
        inactive_placeholder_count=70,
        has_excluded_other_leg=True,
        volume_24h=950_000.0,
        legs=[
            _mk_leg("polymarket", "c1", "Candidate A", 0.50, size=500.0, fee_rate=0.04, fees_enabled=True),
            _mk_leg("polymarket", "c2", "Candidate B", 0.453, size=500.0, fee_rate=0.04, fees_enabled=True),
        ],
    )
    res = scan_dutch_books([ev])
    assert len(res) == 1
    opp = res[0]
    assert opp.is_truly_exhaustive is False
    assert opp.exhaustiveness_status == "NON_EXHAUSTIVE_AUGMENTED_TRAP"
    assert opp.is_net_arb is False
    assert "negRiskAugmented" in opp.exhaustiveness_reason


def test_dutch_book_skips_event_with_missing_leg_ask_no_fake_zero() -> None:
    ev = MultiOutcomeEvent(
        venue="polymarket",
        currency="USD",
        event_id="pm-missing",
        title="Incomplete Quotes Event",
        is_mutually_exclusive=True,
        neg_risk=True,
        neg_risk_augmented=False,
        total_markets_in_event=2,
        active_markets_count=2,
        legs=[
            _mk_leg("polymarket", "l1", "A", 0.45),
            _mk_leg("polymarket", "l2", "B", None),  # Missing ask must NOT become 0.0!
        ],
    )
    assert scan_dutch_books([ev]) == []


# ---------------------------------------------------------------------------
# 3. Cross-Venue YES+NO Arb, Play-Money Isolation & False-Match Rejection
# ---------------------------------------------------------------------------


def test_cross_venue_pure_cash_arb_and_annualized_roi() -> None:
    # Mirrors real Josh Shapiro 2028 Dem Nominee cross-venue spread
    pm = NormalizedMarket(
        venue="polymarket",
        currency="USD",
        market_id="pm-shapiro",
        event_id="pm-dem28",
        event_title="Democratic Presidential Nominee 2028",
        question="Will Josh Shapiro win the 2028 Democratic presidential nomination?",
        outcome_label="Josh Shapiro",
        yes_bid=0.064,
        yes_ask=0.065,
        no_bid=0.935,
        no_ask=0.936,
        mid_prob=0.0645,
        yes_ask_size=5000.0,
        no_ask_size=1200.0,
        volume_24h=80_000.0,
        liquidity=300_000.0,
        fees_enabled=True,
        taker_fee_rate=0.04,
        end_date_iso="2028-11-08T04:59:00Z",
    )
    ks = NormalizedMarket(
        venue="kalshi",
        currency="USD",
        market_id="KXPRESNOMD-28-JS",
        event_id="KXPRESNOMD-28",
        event_title="2028 Democratic presidential nominee",
        question="Will Josh Shapiro be the 2028 Democratic presidential nominee?",
        outcome_label="Josh Shapiro",
        yes_bid=0.047,
        yes_ask=0.048,
        no_bid=0.952,
        no_ask=0.953,
        mid_prob=0.0475,
        yes_ask_size=3000.0,
        no_ask_size=4000.0,
        volume_24h=20_000.0,
        liquidity=150_000.0,
        fees_enabled=True,
        taker_fee_rate=0.07,
        end_date_iso="2028-11-08T04:59:00Z",
    )
    now_ref = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
    matches = match_cross_venue([pm, ks], min_divergence_pp=3.0, now_utc=now_ref)
    assert len(matches) == 1
    m = matches[0]
    assert m.is_cash_arb_eligible is True
    assert m.signal_class == "PURE_CROSS_VENUE_ARB"
    assert m.leg_a_action == "BUY NO"  # Polymarket NO @ 0.936
    assert m.leg_b_action == "BUY YES"  # Kalshi YES @ 0.048
    assert m.gross_cost == pytest.approx(0.984, abs=1e-6)
    expected_pm_fee = 0.04 * 0.936 * 0.064
    expected_ks_fee = 0.07 * 0.048 * 0.952
    expected_net = 0.984 + expected_pm_fee + expected_ks_fee
    assert m.net_cost == pytest.approx(expected_net, abs=1e-5)
    assert m.net_roi_pct is not None and m.net_roi_pct > 1.0
    assert m.max_fillable_shares == pytest.approx(1200.0)
    assert m.annualized_net_roi_pct is not None and m.annualized_net_roi_pct < m.net_roi_pct  # > 1yr lockup
    assert "LEGGED_IN_EXECUTION_RISK" in m.risk_flags
    assert "RESOLUTION_ORACLE_BASIS_RISK" in m.risk_flags


def test_manifold_play_money_is_divergence_only_never_cash_arb() -> None:
    pm = NormalizedMarket(
        venue="polymarket",
        currency="USD",
        market_id="pm-jensen",
        event_id="pm-ev-j",
        event_title="Jensen Huang rejoins NVIDIA by...?",
        question="Will Jensen Huang rejoin NVIDIA by December 31, 2026?",
        outcome_label="December 31, 2026",
        yes_bid=0.051,
        yes_ask=0.053,
        no_ask=0.949,
        mid_prob=0.052,
        volume_24h=600_000.0,
        liquidity=400_000.0,
    )
    mf = NormalizedMarket(
        venue="manifold",
        currency="MANA_PLAY_MONEY",
        market_id="mf-jensen",
        event_id="mf-jensen",
        event_title="Will Jensen Huang return as NVIDIA CEO by the end of 2026?",
        question="Will Jensen Huang return as NVIDIA CEO by the end of 2026?",
        yes_bid=0.11,
        yes_ask=0.11,
        no_ask=0.89,
        mid_prob=0.11,
        volume_24h=25_000.0,
        liquidity=3_500.0,
    )
    matches = match_cross_venue([pm, mf], min_divergence_pp=3.0)
    assert len(matches) == 1
    m = matches[0]
    assert m.is_cash_arb_eligible is False
    assert m.signal_class == "HIGH_LIQ_DIVERGENCE"
    assert m.prob_diff_pp == pytest.approx(5.8, abs=0.05)
    assert m.net_cost is None
    assert "PLAY_MONEY_BENCHMARK_NOT_CASH_ARB" in m.risk_flags


def test_false_match_rejections_year_month_threshold_direction_polarity_candidate() -> None:
    def _mk(venue: str, mid: str, ev: str, q: str, out: str = "", end: str | None = None) -> NormalizedMarket:
        return NormalizedMarket(
            venue=venue,
            currency="USD",
            market_id=mid,
            event_id=mid,
            event_title=ev,
            question=q,
            outcome_label=out,
            yes_ask=0.40,
            no_ask=0.61,
            mid_prob=0.40,
            volume_24h=10_000.0,
            liquidity=10_000.0,
            end_date_iso=end,
        )

    # 1. Wrong year: Recession 2026 vs 2027
    p1 = extract_proposition(_mk("polymarket", "r26", "US Recession 2026", "Will the US enter a recession in 2026?"))
    p2 = extract_proposition(_mk("kalshi", "r27", "Recession in 2027?", "Will there be a US recession in 2027?"))
    assert propositions_compatible(p1, p2)[0] is False

    # 2. Wrong month: Fed Decision Dec 2026 vs Jan 2027
    f1 = extract_proposition(
        _mk("polymarket", "fd1", "Fed Decision in December?", "Will the Fed increase rates by 25 bps after December 2026?", "25 bps increase", "2026-12-10T04:59:00Z")
    )
    f2 = extract_proposition(
        _mk("kalshi", "fd2", "Fed decision in Jan 2027?", "Will the Federal Reserve Hike rates by 25bps at their January 2027 meeting?", "Hike 25bps", "2027-01-28T04:59:00Z")
    )
    assert propositions_compatible(f1, f2)[0] is False

    # 3. Wrong numeric threshold: BTC $100,000 vs $120,000 -> REJECT; BTC $99,999.99 vs $100,000 -> ACCEPT!
    b100_pm = extract_proposition(_mk("polymarket", "b1", "What price will Bitcoin hit in 2026?", "Will Bitcoin reach $100,000 by December 31, 2026?", "↑ 100,000"))
    b120_ks = extract_proposition(_mk("kalshi", "b2", "How high will Bitcoin get in 2026?", "Will Bitcoin be above $119,999.99 by Dec 31, 2026?", "Above $119,999.99"))
    b100_ks = extract_proposition(_mk("kalshi", "b3", "How high will Bitcoin get in 2026?", "Will Bitcoin be above $99,999.99 by Dec 31, 2026?", "Above $99,999.99"))
    assert propositions_compatible(b100_pm, b120_ks)[0] is False
    assert propositions_compatible(b100_pm, b100_ks)[0] is True

    # 4. Opposite direction: BTC above $55,000 vs below $55,000 -> REJECT!
    b_up = extract_proposition(_mk("polymarket", "bu", "What price will Bitcoin hit in 2026?", "Will Bitcoin reach $55,000 by December 31, 2026?", "↑ 55,000"))
    b_dn = extract_proposition(_mk("kalshi", "bd", "How low will Bitcoin get in 2026?", "Will Bitcoin be below $55000.00 by Jan 1, 2027 at 12:00am ET?", "Below $55,000.00"))
    assert propositions_compatible(b_up, b_dn)[0] is False

    # 5. Opposite polarity: Putin out of power vs Putin remains President -> REJECT!
    pol_out = extract_proposition(_mk("polymarket", "po", "Next leader out of power before 2027?", "Will Vladimir Putin be the next leader out of power before 2027?"))
    pol_in = extract_proposition(_mk("manifold", "pi", "Will Vladimir Putin be President of Russia at the end of 2026?", "Will Vladimir Putin be President of Russia at the end of 2026?"))
    assert propositions_compatible(pol_out, pol_in)[0] is False

    # 6. Mismatched candidate in general_binary -> REJECT!
    cand_a = extract_proposition(_mk("polymarket", "ca", "Next Prime Minister of Israel?", "Will Yair Golan be the next Prime Minister of Israel?", "Yair Golan", "2026-12-31T00:00:00Z"))
    cand_b = extract_proposition(_mk("kalshi", "cb", "Next Prime Minister of Israel?", "Will Yair Lapid be the next Prime Minister of Israel?", "Yair Lapid", "2026-12-31T00:00:00Z"))
    assert propositions_compatible(cand_a, cand_b)[0] is False


def test_kalshi_graceful_degradation_on_auth_block() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden by policy")

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as client:
        mkts, multi, status = fetch_kalshi_data(client, series_tickers=("KXFEDDECISION",), general_pages=1)
    assert mkts == []
    assert multi == []
    assert status.ok is False
    assert status.http_status == 403
    assert status.error is not None and "403" in status.error


# ---------------------------------------------------------------------------
# 4. Discord Embeds, Deduplication & Dashboard XSS Security Tests
# ---------------------------------------------------------------------------


def test_discord_embeds_and_alert_deduplication(tmp_path: Path) -> None:
    pm = NormalizedMarket(
        venue="polymarket",
        currency="USD",
        market_id="pm-fed-h50",
        event_id="pm-fed",
        event_title="Fed Decision in December?",
        question="Will the Fed increase interest rates by 50+ bps after the December 2026 meeting?",
        outcome_label="50+ bps increase",
        yes_bid=0.041,
        yes_ask=0.042,
        no_ask=0.959,
        mid_prob=0.0415,
        yes_ask_size=2500.0,
        volume_24h=500_000.0,
        liquidity=300_000.0,
        fees_enabled=True,
        taker_fee_rate=0.05,
        end_date_iso="2026-12-10T04:59:00Z",
        url="https://polymarket.com/event/fed-decision",
    )
    ks = NormalizedMarket(
        venue="kalshi",
        currency="USD",
        market_id="KXFEDDECISION-26DEC-H26",
        event_id="KXFEDDECISION-26DEC",
        event_title="Fed decision in Dec 2026?",
        question="Will the Federal Reserve Hike rates by >25bps at their December 2026 meeting?",
        outcome_label="Hike >25bps",
        yes_bid=0.050,
        yes_ask=0.060,
        no_bid=0.940,
        no_ask=0.950,
        mid_prob=0.055,
        no_ask_size=3000.0,
        volume_24h=220_000.0,
        liquidity=100_000.0,
        fees_enabled=True,
        taker_fee_rate=0.07,
        end_date_iso="2026-12-10T04:59:00Z",
        url="https://kalshi.com/markets/kxfeddecision",
    )
    matches = match_cross_venue([pm, ks], now_utc=datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc))
    assert len(matches) == 1
    m = matches[0]
    assert m.signal_class == "PURE_CROSS_VENUE_ARB"

    embed = format_cross_venue_discord_embed(m)
    assert "[PURE CROSS-VENUE ARB]" in embed["title"]
    assert "NOT financial advice" in embed["footer"]["text"]
    assert any(f["name"] == "Execution & Basis Risk Flags" for f in embed["fields"])

    tg_msg = format_cross_venue_telegram_html(m)
    assert "PURE_CROSS_VENUE_ARB" in tg_msg

    # Deduplication store behavior
    state_file = tmp_path / "alert_state.json"
    dedup = AlertDeduplicator(state_path=state_file, cooldown_seconds=3600, min_edge_improvement_bps=100)
    assert dedup.should_alert("k1", edge_bps=50, now_ts=1000.0) is True
    dedup.record_sent("k1", "PURE_CROSS_VENUE_ARB", "Fed", edge_bps=50, now_ts=1000.0)
    # Same edge inside cooldown -> suppressed!
    assert dedup.should_alert("k1", edge_bps=80, now_ts=1500.0) is False
    # Material edge improvement (+100 bps) -> re-triggers early!
    assert dedup.should_alert("k1", edge_bps=155, now_ts=1500.0) is True
    # Past cooldown -> re-triggers cleanly!
    assert dedup.should_alert("k1", edge_bps=50, now_ts=4700.0) is True

    # Reload from persisted JSON & verify dry-run dispatch
    report = ScanReport(
        generated_at_utc="2026-11-20T06:00:00Z",
        scan_duration_sec=1.2,
        cross_venue_matches=matches,
    )
    dedup2 = AlertDeduplicator(state_path=state_file, cooldown_seconds=3600)
    res1 = dispatch_alerts(report, deduplicator=dedup2, dry_run=True)
    assert res1["dry_run"] is True
    assert res1["alerts_selected"] == 1
    assert res1["discord_payload"] is not None
    # Immediate second dispatch must be deduplicated to 0!
    res2 = dispatch_alerts(report, deduplicator=dedup2, dry_run=True)
    assert res2["alerts_selected"] == 0


def test_dashboard_html_escapes_xss_and_script_breakout() -> None:
    xss_title = 'Evil Market </script><script>alert("xss")</script><img src=x onerror=alert(1)>'
    ev = MultiOutcomeEvent(
        venue="polymarket",
        currency="USD",
        event_id="xss-1",
        title=xss_title,
        url="javascript:alert(1)",
        is_mutually_exclusive=True,
        neg_risk=True,
        neg_risk_augmented=True,
        total_markets_in_event=5,
        active_markets_count=2,
        inactive_placeholder_count=3,
        legs=[
            _mk_leg("polymarket", "x1", "<b>Leg1</b>", 0.40),
            _mk_leg("polymarket", "x2", "<script>bad()</script>", 0.40),
        ],
    )
    dutch = scan_dutch_books([ev])
    embed = format_dutch_book_discord_embed(dutch[0])
    report = ScanReport(
        generated_at_utc="2026-11-20T06:00:00Z",
        scan_duration_sec=0.5,
        venue_statuses=[
            VenueStatus(
                venue="polymarket",
                endpoint="https://gamma-api.polymarket.com/events",
                ok=False,
                http_status=500,
                error='<img src=x onerror="alert(1)">',
            )
        ],
        summary={"pure_cross_venue_arbs": 0, "total_markets_scanned": 2},
        dutch_books=dutch,
        sample_discord_embeds=[embed],
    )
    out_html = render_dashboard_html(report)
    assert "</script><script>alert" not in out_html
    assert '<img src=x onerror="alert(1)">' not in out_html
    assert "<img src=x onerror=alert(1)>" not in out_html
    # Confirm JSON script tag safely escaped '<' as '\u003c'
    assert "\\u003c/script\\u003e" in out_html


# ---------------------------------------------------------------------------
# 5. Live Zero-Key Public API Smoke Test
# ---------------------------------------------------------------------------


def test_live_zero_key_public_scan_smoke() -> None:
    """Verify real unauthenticated live scan returns valid data & provenance."""
    report = run_live_scan(
        max_polymarket_events=60,
        kalshi_series=("KXFEDDECISION", "KXPRESNOMD", "KXPRESPARTY"),
        kalshi_general_pages=0,
        manifold_limit_per_sort=20,
        manifold_expand_multichoice=2,
        enrich_orderbooks=True,
        max_dutch_books_to_enrich=1,
        max_matches_to_enrich=2,
    )
    assert report.summary["venues_online"] >= 2
    assert report.summary["total_markets_scanned"] > 50
    by_venue = {v.venue: v for v in report.venue_statuses}
    assert by_venue["polymarket"].http_status == 200
    assert by_venue["kalshi"].http_status == 200
    assert by_venue["manifold"].http_status == 200
    assert report.summary["cross_venue_matches"] >= 5
    rendered = render_dashboard_html(report)
    assert "PredMarket Arb Radar" in rendered
    assert "latest_scan.json" in rendered
