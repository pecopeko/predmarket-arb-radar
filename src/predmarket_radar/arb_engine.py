"""Quantitative arbitrage, Dutch-book exhaustiveness, and cross-venue matching engine.

Implements:
1. Exact exchange taker fee schedules:
   - Kalshi: ceil_cent(multiplier * 0.07 * C * P * (1 - P)) (marginal 0.07 * P * (1 - P))
   - Polymarket: feeSchedule.rate * P * (1 - P) when feesEnabled=True (0.0 on fee-free markets)
2. Intra-venue multi-outcome Dutch-book detection with strict exhaustiveness auditing
   (separating true exhaustive baskets from Polymarket negRiskAugmented / open-field traps).
3. Cross-platform binary proposition matcher with strict year/month/threshold/direction/polarity
   rejection, pure YES+NO fee-adjusted cash arb calculation, annualized ROI, and
   high-liquidity probability divergence detection.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from .models import (
    CrossVenueMatch,
    DutchBookLeg,
    DutchBookOpportunity,
    ExhaustivenessStatus,
    MultiOutcomeEvent,
    NormalizedMarket,
    SignalClass,
)
from .venues import fetch_kalshi_orderbook, fetch_polymarket_clob_book

# ---------------------------------------------------------------------------
# Exact Exchange Fee Schedules
# ---------------------------------------------------------------------------


def kalshi_taker_fee_per_contract(
    price: float,
    fee_rate: float = 0.07,
    multiplier: float = 1.0,
) -> float:
    """Per-contract marginal Kalshi taker fee: multiplier * 0.07 * P * (1 - P).

    Peaks at $0.0175/contract at P = 0.50 (for multiplier=1.0) and approaches $0
    at the price extremes.
    """
    p = min(max(price, 0.0), 1.0)
    return round(multiplier * fee_rate * p * (1.0 - p), 6)


def kalshi_order_taker_fee(
    price: float,
    contracts: float,
    fee_rate: float = 0.07,
    multiplier: float = 1.0,
) -> float:
    """Exact order-level Kalshi taker fee rounded UP to the nearest cent."""
    if contracts <= 0.0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    raw = multiplier * fee_rate * contracts * p * (1.0 - p)
    return math.ceil(raw * 100.0 - 1e-12) / 100.0


def polymarket_taker_fee_per_share(
    price: float,
    fee_rate: float = 0.0,
    fees_enabled: bool = False,
) -> float:
    """Per-share Polymarket taker fee: fee_rate * P * (1 - P) when feesEnabled=True."""
    if not fees_enabled or fee_rate <= 0.0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    return round(fee_rate * p * (1.0 - p), 6)


def market_taker_fee(market: NormalizedMarket, price: float) -> float:
    """Compute per-share/contract taker fee using the market's venue & API fee metadata."""
    if market.venue == "kalshi":
        return kalshi_taker_fee_per_contract(price, fee_rate=market.taker_fee_rate or 0.07)
    if market.venue == "polymarket":
        return polymarket_taker_fee_per_share(
            price,
            fee_rate=market.taker_fee_rate,
            fees_enabled=market.fees_enabled,
        )
    return 0.0


def slippage_cost(price: float, slippage_bps: float = 0.0) -> float:
    """Optional conservative slippage buffer per share."""
    if slippage_bps <= 0.0:
        return 0.0
    return round(price * (slippage_bps / 10_000.0), 6)


# ---------------------------------------------------------------------------
# Intra-Venue Multi-Outcome Dutch-Book Scanner
# ---------------------------------------------------------------------------

KALSHI_CLOSED_FINITE_SERIES_PREFIXES = (
    "KXFEDDECISION",
    "KXNBA",
    "KXSB",
    "KXNHL",
    "KXMLB",
    "KXUCL",
    "KXWC",
)


def _assess_exhaustiveness(ev: MultiOutcomeEvent) -> tuple[bool, ExhaustivenessStatus, str]:
    """Audit whether buying YES across all active legs guarantees a $1.00 payout.

    Distinguishes true mutually-exclusive exhaustive baskets from Polymarket
    `negRiskAugmented=True` events (where inactive placeholder slots or an
    excluded `negRiskOther` market absorb unhedged tail probability) and
    open-ended candidate lists on Kalshi.
    """
    if ev.venue == "polymarket":
        if not ev.neg_risk:
            return (
                False,
                "NON_EXHAUSTIVE_OPEN_FIELD",
                "Event is not a Polymarket NegRisk mutually-exclusive market group.",
            )
        if ev.neg_risk_augmented or ev.inactive_placeholder_count > 0 or ev.has_excluded_other_leg:
            reasons: list[str] = []
            if ev.neg_risk_augmented:
                reasons.append("negRiskAugmented=True (unlisted candidates/Other can absorb payout)")
            if ev.inactive_placeholder_count > 0:
                reasons.append(f"{ev.inactive_placeholder_count} inactive placeholder slots exist")
            if ev.has_excluded_other_leg:
                reasons.append("catch-all 'Other' (negRiskOther) contract is not active/included")
            return (
                False,
                "NON_EXHAUSTIVE_AUGMENTED_TRAP",
                "; ".join(reasons),
            )
        return (
            True,
            "EXHAUSTIVE_EFFICIENT",
            f"Polymarket NegRisk non-augmented basket ({ev.active_markets_count}/{ev.total_markets_in_event} active, 0 placeholders).",
        )

    if ev.venue == "kalshi":
        if not ev.is_mutually_exclusive:
            return (
                False,
                "NON_EXHAUSTIVE_OPEN_FIELD",
                "Kalshi event has mutually_exclusive=False.",
            )
        eid_upper = ev.event_id.upper()
        title_lower = ev.title.lower()
        is_closed_league_or_bracket = any(
            eid_upper.startswith(pref) for pref in KALSHI_CLOSED_FINITE_SERIES_PREFIXES
        )
        # Check if legs form a full continuous bracket (e.g. > / < tails) or contain an explicit Other slot.
        labels_lower = " ".join((leg.outcome_label or leg.question).lower() for leg in ev.legs)
        has_two_tails = (">" in labels_lower or "more" in labels_lower or "+" in labels_lower) and (
            "<" in labels_lower or "cut" in labels_lower or "less" in labels_lower or "0" in labels_lower
        )
        if ev.has_tradable_other_leg:
            return (
                True,
                "EXHAUSTIVE_EFFICIENT",
                "Kalshi mutually_exclusive=True with explicit tradable 'Other' catch-all contract.",
            )
        if is_closed_league_or_bracket or (has_two_tails and "who will" not in title_lower):
            return (
                True,
                "EXHAUSTIVE_EFFICIENT",
                f"Kalshi mutually_exclusive=True closed finite outcome set ({ev.active_markets_count} legs).",
            )
        return (
            False,
            "NON_EXHAUSTIVE_OPEN_FIELD",
            "Kalshi mutually_exclusive=True open candidate field without an explicit 'Other' catch-all leg.",
        )

    return False, "NON_EXHAUSTIVE_OPEN_FIELD", "Unsupported venue for cash Dutch-book."


def scan_dutch_books(
    events: list[MultiOutcomeEvent],
    slippage_bps: float = 0.0,
    include_efficient_exhaustive: bool = True,
    max_efficient_overround: float = 1.08,
) -> list[DutchBookOpportunity]:
    """Scan multi-outcome events for intra-venue Dutch-book arbitrage & trap warnings."""
    results: list[DutchBookOpportunity] = []

    for ev in events:
        if ev.currency != "USD" or len(ev.legs) < 2:
            continue

        # Every active leg MUST have a valid executable yes_ask — never fill missing asks with 0!
        if any(leg.yes_ask is None or leg.yes_ask <= 0.0 for leg in ev.legs):
            continue

        is_exhaustive, base_status, reason = _assess_exhaustiveness(ev)

        dutch_legs: list[DutchBookLeg] = []
        sum_asks = 0.0
        sum_fees = 0.0
        sum_slip = 0.0
        sizes: list[float] = []
        book_verified_count = 0

        for leg in ev.legs:
            ask = float(leg.yes_ask)  # guaranteed non-None above
            fee = market_taker_fee(leg, ask)
            slip = slippage_cost(ask, slippage_bps)
            sum_asks += ask
            sum_fees += fee
            sum_slip += slip
            has_sz = leg.yes_ask_size is not None and leg.yes_ask_size > 0.0
            if has_sz:
                sizes.append(float(leg.yes_ask_size))
                book_verified_count += 1
            dutch_legs.append(
                DutchBookLeg(
                    market_id=leg.market_id,
                    label=leg.outcome_label or leg.question,
                    yes_ask=round(ask, 6),
                    taker_fee=round(fee, 6),
                    yes_ask_size=round(float(leg.yes_ask_size), 2) if has_sz else None,
                    token_id=leg.yes_token_id,
                    book_verified=has_sz,
                )
            )

        all_in = sum_asks + sum_fees + sum_slip
        gross_edge_pct = round((1.0 - sum_asks) * 100.0, 4)
        net_roi_pct = round(((1.0 - all_in) / all_in) * 100.0, 4) if all_in > 0 else 0.0
        is_net_arb = is_exhaustive and (all_in < 1.0)

        status: ExhaustivenessStatus
        if is_exhaustive:
            status = "EXHAUSTIVE_DUTCH_BOOK" if sum_asks < 1.0 else "EXHAUSTIVE_EFFICIENT"
        else:
            status = "NON_EXHAUSTIVE_AUGMENTED_TRAP" if sum_asks < 1.0 else base_status

        # Keep: (a) any exhaustive basket with sum_asks <= max_efficient_overround, or
        # (b) any non-exhaustive trap where sum_asks < 1.0 (valuable warning for traders!).
        if is_exhaustive:
            if not include_efficient_exhaustive and sum_asks >= 1.0:
                continue
            if sum_asks > max_efficient_overround:
                continue
        else:
            if sum_asks >= 1.0:
                continue

        max_shares: Optional[float] = None
        max_usd: Optional[float] = None
        exp_profit: Optional[float] = None
        if book_verified_count == len(dutch_legs) and sizes:
            max_shares = round(min(sizes), 2)
            max_usd = round(max_shares * all_in, 2)
            exp_profit = round(max_shares * (1.0 - all_in), 2)

        results.append(
            DutchBookOpportunity(
                event_id=ev.event_id,
                venue=ev.venue,
                title=ev.title,
                url=ev.url,
                category=ev.category,
                end_date_iso=ev.end_date_iso,
                exhaustiveness_status=status,
                is_truly_exhaustive=is_exhaustive,
                exhaustiveness_reason=reason,
                leg_count=len(dutch_legs),
                total_markets_in_event=ev.total_markets_in_event,
                inactive_placeholder_count=ev.inactive_placeholder_count,
                sum_yes_asks=round(sum_asks, 6),
                total_taker_fees=round(sum_fees, 6),
                slippage_buffer=round(sum_slip, 6),
                total_all_in_cost=round(all_in, 6),
                gross_edge_pct=gross_edge_pct,
                net_roi_pct=net_roi_pct,
                is_net_arb=is_net_arb,
                book_verified_legs=book_verified_count,
                max_fillable_shares=max_shares,
                max_fillable_usd=max_usd,
                expected_net_profit_usd=exp_profit,
                volume_24h=round(ev.volume_24h, 2),
                liquidity=round(ev.liquidity, 2),
                legs=dutch_legs,
            )
        )

    # Sort: true net arbs first, then exhaustive near-arbs, then non-exhaustive trap warnings.
    order_rank = {
        "EXHAUSTIVE_DUTCH_BOOK": 0,
        "EXHAUSTIVE_EFFICIENT": 1,
        "NON_EXHAUSTIVE_AUGMENTED_TRAP": 2,
        "NON_EXHAUSTIVE_OPEN_FIELD": 3,
    }
    results.sort(key=lambda x: (order_rank.get(x.exhaustiveness_status, 9), x.total_all_in_cost))
    return results


def enrich_dutch_book_orderbooks(
    client: httpx.Client,
    opportunities: list[DutchBookOpportunity],
    max_events_to_enrich: int = 4,
    max_legs_per_event: int = 15,
) -> int:
    """Fetch live CLOB orderbooks for top exhaustive Polymarket Dutch-book candidates."""
    books_fetched = 0
    enriched_events = 0
    candidates = [
        opp
        for opp in opportunities
        if opp.venue == "polymarket"
        and opp.is_truly_exhaustive
        and opp.leg_count <= max_legs_per_event
        and opp.book_verified_legs < opp.leg_count
    ]
    # Prioritize true net arbs first, then highest 24h volume flagship baskets (e.g. Fed Decision).
    candidates.sort(key=lambda o: (0 if o.is_net_arb else 1, -o.volume_24h, o.total_all_in_cost))

    for opp in candidates:
        if enriched_events >= max_events_to_enrich:
            break

        sizes: list[float] = []
        verified = 0
        recomputed_sum_asks = 0.0
        recomputed_fees = 0.0
        all_ok = True

        for leg in opp.legs:
            if not leg.token_id:
                all_ok = False
                continue
            snap = fetch_polymarket_clob_book(client, token_id=leg.token_id, market_id=leg.market_id)
            books_fetched += 1
            if snap.error is None and snap.best_ask is not None and snap.best_ask_size is not None:
                # Preserve fee ratio implied by original leg metadata.
                orig_denom = leg.yes_ask * (1.0 - leg.yes_ask)
                inferred_rate = (leg.taker_fee / orig_denom) if orig_denom > 1e-9 else 0.0
                leg.yes_ask = round(snap.best_ask, 6)
                leg.yes_ask_size = round(snap.best_ask_size, 2)
                leg.taker_fee = round(inferred_rate * leg.yes_ask * (1.0 - leg.yes_ask), 6)
                leg.book_verified = True
                sizes.append(leg.yes_ask_size)
                verified += 1
            else:
                all_ok = False
            recomputed_sum_asks += leg.yes_ask
            recomputed_fees += leg.taker_fee

        opp.book_verified_legs = verified
        if verified > 0:
            opp.sum_yes_asks = round(recomputed_sum_asks, 6)
            opp.total_taker_fees = round(recomputed_fees, 6)
            opp.total_all_in_cost = round(recomputed_sum_asks + recomputed_fees + opp.slippage_buffer, 6)
            opp.gross_edge_pct = round((1.0 - opp.sum_yes_asks) * 100.0, 4)
            opp.net_roi_pct = (
                round(((1.0 - opp.total_all_in_cost) / opp.total_all_in_cost) * 100.0, 4)
                if opp.total_all_in_cost > 0
                else 0.0
            )
            opp.is_net_arb = opp.is_truly_exhaustive and (opp.total_all_in_cost < 1.0)
            if opp.is_truly_exhaustive:
                opp.exhaustiveness_status = (
                    "EXHAUSTIVE_DUTCH_BOOK" if opp.sum_yes_asks < 1.0 else "EXHAUSTIVE_EFFICIENT"
                )
        if all_ok and verified == opp.leg_count and sizes:
            opp.max_fillable_shares = round(min(sizes), 2)
            opp.max_fillable_usd = round(opp.max_fillable_shares * opp.total_all_in_cost, 2)
            opp.expected_net_profit_usd = round(
                opp.max_fillable_shares * (1.0 - opp.total_all_in_cost),
                2,
            )
        enriched_events += 1

    return books_fetched


# ---------------------------------------------------------------------------
# Cross-Platform Entity / Threshold / Date / Polarity Feature Extraction
# ---------------------------------------------------------------------------

MONTH_MAP: dict[str, str] = {
    "jan": "january",
    "january": "january",
    "feb": "february",
    "february": "february",
    "mar": "march",
    "march": "march",
    "apr": "april",
    "april": "april",
    "may": "may",
    "jun": "june",
    "june": "june",
    "jul": "july",
    "july": "july",
    "aug": "august",
    "august": "august",
    "sep": "september",
    "sept": "september",
    "september": "september",
    "oct": "october",
    "october": "october",
    "nov": "november",
    "november": "november",
    "dec": "december",
    "december": "december",
}

NBA_TEAM_ALIASES: dict[str, str] = {
    "atlanta": "atlanta hawks",
    "atlanta hawks": "atlanta hawks",
    "boston": "boston celtics",
    "boston celtics": "boston celtics",
    "brooklyn": "brooklyn nets",
    "brooklyn nets": "brooklyn nets",
    "charlotte": "charlotte hornets",
    "charlotte hornets": "charlotte hornets",
    "chicago": "chicago bulls",
    "chicago bulls": "chicago bulls",
    "cleveland": "cleveland cavaliers",
    "cleveland cavaliers": "cleveland cavaliers",
    "dallas": "dallas mavericks",
    "dallas mavericks": "dallas mavericks",
    "denver": "denver nuggets",
    "denver nuggets": "denver nuggets",
    "detroit": "detroit pistons",
    "detroit pistons": "detroit pistons",
    "golden state": "golden state warriors",
    "golden state warriors": "golden state warriors",
    "houston": "houston rockets",
    "houston rockets": "houston rockets",
    "indiana": "indiana pacers",
    "indiana pacers": "indiana pacers",
    "la clippers": "los angeles clippers",
    "los angeles c": "los angeles clippers",
    "los angeles clippers": "los angeles clippers",
    "la lakers": "los angeles lakers",
    "los angeles l": "los angeles lakers",
    "los angeles lakers": "los angeles lakers",
    "memphis": "memphis grizzlies",
    "memphis grizzlies": "memphis grizzlies",
    "miami": "miami heat",
    "miami heat": "miami heat",
    "milwaukee": "milwaukee bucks",
    "milwaukee bucks": "milwaukee bucks",
    "minnesota": "minnesota timberwolves",
    "minnesota timberwolves": "minnesota timberwolves",
    "new orleans": "new orleans pelicans",
    "new orleans pelicans": "new orleans pelicans",
    "new york": "new york knicks",
    "new york knicks": "new york knicks",
    "oklahoma city": "oklahoma city thunder",
    "oklahoma city thunder": "oklahoma city thunder",
    "orlando": "orlando magic",
    "orlando magic": "orlando magic",
    "philadelphia": "philadelphia 76ers",
    "philadelphia 76ers": "philadelphia 76ers",
    "phoenix": "phoenix suns",
    "phoenix suns": "phoenix suns",
    "portland": "portland trail blazers",
    "portland trail blazers": "portland trail blazers",
    "sacramento": "sacramento kings",
    "sacramento kings": "sacramento kings",
    "san antonio": "san antonio spurs",
    "san antonio spurs": "san antonio spurs",
    "toronto": "toronto raptors",
    "toronto raptors": "toronto raptors",
    "utah": "utah jazz",
    "utah jazz": "utah jazz",
    "washington": "washington wizards",
    "washington wizards": "washington wizards",
}

UCL_CLUB_ALIASES: dict[str, str] = {
    "psg": "paris saint germain",
    "paris saint-germain": "paris saint germain",
    "paris saint germain": "paris saint germain",
    "inter": "inter milan",
    "inter milan": "inter milan",
    "internazionale": "inter milan",
    "man city": "manchester city",
    "manchester city": "manchester city",
    "man united": "manchester united",
    "manchester united": "manchester united",
    "bayern": "bayern munich",
    "bayern munich": "bayern munich",
    "fc bayern munich": "bayern munich",
    "real madrid": "real madrid",
    "barcelona": "barcelona",
    "fc barcelona": "barcelona",
    "arsenal": "arsenal",
    "liverpool": "liverpool",
    "chelsea": "chelsea",
    "atletico madrid": "atletico madrid",
    "atlético madrid": "atletico madrid",
    "borussia dortmund": "borussia dortmund",
    "dortmund": "borussia dortmund",
    "juventus": "juventus",
    "ac milan": "ac milan",
    "napoli": "napoli",
    "aston villa": "aston villa",
    "tottenham": "tottenham hotspur",
    "tottenham hotspur": "tottenham hotspur",
    "newcastle": "newcastle united",
    "newcastle united": "newcastle united",
    "bayer leverkusen": "bayer leverkusen",
    "leverkusen": "bayer leverkusen",
    "benfica": "benfica",
    "sporting cp": "sporting cp",
    "sporting lisbon": "sporting cp",
    "porto": "fc porto",
    "fc porto": "fc porto",
    "ajax": "ajax",
    "villarreal": "villarreal",
    "atalanta": "atalanta",
    "rb leipzig": "rb leipzig",
}

PERSON_ALIASES: dict[str, str] = {
    "aoc": "alexandria ocasio cortez",
    "alexandria ocasio-cortez": "alexandria ocasio cortez",
    "alexandria ocasio cortez": "alexandria ocasio cortez",
    "jd vance": "jd vance",
    "j.d. vance": "jd vance",
    "j d vance": "jd vance",
    "stephen a. smith": "stephen a smith",
    "stephen a smith": "stephen a smith",
    "donald trump jr.": "donald trump jr",
    "donald trump jr": "donald trump jr",
    "rfk jr": "robert f kennedy jr",
    "rfk jr.": "robert f kennedy jr",
    "robert f. kennedy jr.": "robert f kennedy jr",
    "jb pritzker": "jb pritzker",
    "j.b. pritzker": "jb pritzker",
}

STOPWORDS = frozenset(
    {
        "will",
        "the",
        "be",
        "in",
        "of",
        "to",
        "a",
        "an",
        "on",
        "at",
        "by",
        "for",
        "or",
        "and",
        "is",
        "are",
        "this",
        "that",
        "what",
        "which",
        "who",
        "when",
        "before",
        "after",
        "end",
        "year",
        "next",
        "us",
        "u",
        "s",
        "win",
        "wins",
        "winner",
        "champion",
        "championship",
        "become",
        "hit",
        "reach",
        "get",
        "price",
        "market",
        "yes",
        "no",
    }
)

NEGATION_RE = re.compile(
    r"\b(not|never|fail|fails|failed|lose|loses|lost|without|won't|wont|doesn't|doesnt|"
    r"cannot|can't|cant|avoid|avoids|no\s+recession|out\s+of\s+power|out\s+before|"
    r"leave\s+office|leaves\s+office|ousted|removed|resign|resigns|step\s+down|impeached)\b",
    re.IGNORECASE,
)


@dataclass
class CanonicalProposition:
    market: NormalizedMarket
    family: str
    entity_key: str
    canonical_label: str
    target_year: Optional[int] = None
    target_month: Optional[str] = None
    threshold_value: Optional[float] = None
    threshold_direction: Optional[str] = None
    is_negated: bool = False
    outcome_key: str = ""
    tokens: frozenset[str] = field(default_factory=frozenset)
    rule_basis_note: Optional[str] = None


def _clean_text(s: str) -> str:
    s = s.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"[^a-z0-9\s\.\$\+\-><]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_person(raw: str) -> str:
    s = raw.lower().replace("’", "'")
    s = re.sub(r"[^a-z0-9\s\.\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s in PERSON_ALIASES:
        return PERSON_ALIASES[s]
    s_nodot = s.replace(".", "").replace("-", " ")
    s_nodot = re.sub(r"\s+", " ", s_nodot).strip()
    return PERSON_ALIASES.get(s_nodot, s_nodot)


def _extract_target_year_and_month(text: str, end_date_iso: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Extract target year & month cleanly, normalizing 'by Jan 1, YYYY 12:00am' -> end of YYYY-1."""
    low = text.lower()

    # Special case: "by Jan 1, 2027 at 12:00am" or "before Jan 1, 2027" == end of 2026!
    m_ny = re.search(
        r"(?:by|before)\s+(?:jan(?:uary)?\s*1(?:st)?,?\s*(20\d{2})|(20\d{2}))(?:\s+at\s+12:00\s*am)?",
        low,
    )
    if m_ny:
        if m_ny.group(1):
            return int(m_ny.group(1)) - 1, "year_end"
        if m_ny.group(2) and "before" in m_ny.group(0):
            return int(m_ny.group(2)) - 1, "year_end"

    # Special case: "by Dec 31, 2026" or "end of 2026" -> (2026, "year_end")
    m_ye = re.search(r"(?:dec(?:ember)?\s*31(?:st)?,?\s*(20\d{2})|end\s+of\s+(20\d{2}))", low)
    if m_ye:
        yr = int(m_ye.group(1) or m_ye.group(2))
        return yr, "year_end"

    # Look for explicit year (2025..2099)
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", low)]
    # Handle seasonal formats like "2026-27" -> target championship year 2027
    m_season = re.search(r"\b(20\d{2})\s*-\s*(\d{2})\b", low)
    target_year: Optional[int] = None
    if m_season:
        base_yr = int(m_season.group(1))
        end_two = int(m_season.group(2))
        target_year = (base_yr // 100) * 100 + end_two
    elif years:
        target_year = years[-1]
    elif "this year" in low and end_date_iso and len(end_date_iso) >= 4 and end_date_iso[:4].isdigit():
        target_year = int(end_date_iso[:4])
    elif end_date_iso and len(end_date_iso) >= 4 and end_date_iso[:4].isdigit():
        # Only infer year from endDate when text mentions a month without year (e.g., "Fed Decision in December?")
        if any(re.search(rf"\b{m}\b", low) for m in MONTH_MAP):
            target_year = int(end_date_iso[:4])

    # Extract explicit month (skip if already identified year_end)
    target_month: Optional[str] = None
    for abbr, full in MONTH_MAP.items():
        if re.search(rf"\b{abbr}\b", low):
            target_month = full
            break

    # If a specific calendar day like "Nov 20" or "November 16-22" appears, encode day precision
    # so daily/weekly markets NEVER match annual year-end markets!
    m_day = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:\s*-\s*(\d{1,2}))?\b",
        low,
    )
    if m_day:
        mon = MONTH_MAP[m_day.group(1)]
        d1 = int(m_day.group(2))
        d2 = m_day.group(3)
        if not (mon == "december" and d1 == 31 and d2 is None):
            if d2:
                target_month = f"{mon}_{d1}_{d2}"
            else:
                target_month = f"{mon}_{d1}"

    return target_year, target_month


def _extract_dollar_threshold(text: str) -> Optional[float]:
    """Extract dollar/numeric strike threshold, snapping .99 strikes within $0.05."""
    # Match $100,000 / $99,999.99 / $150k / ↑ 95,000 / ↓ 55,000
    m = re.search(
        r"(?:\$|↑\s*|↓\s*)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(k|m|b)?\b",
        text,
        re.IGNORECASE,
    )
    if not m:
        # Fallback for "above 7,500" or "below 6,100"
        m = re.search(
            r"\b(?:above|below|over|under|reach|dip\s+to|hit)\s+(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(k|m|b)?\b",
            text,
            re.IGNORECASE,
        )
    if not m:
        return None
    raw_num = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        raw_num *= 1_000.0
    elif suffix == "m":
        raw_num *= 1_000_000.0
    elif suffix == "b":
        raw_num *= 1_000_000_000.0

    # Kalshi uses $99,999.99 to represent "strictly above $99,999.99" == "$100,000".
    nearest_int = round(raw_num)
    if abs(raw_num - nearest_int) <= 0.05 and nearest_int >= 100:
        return float(nearest_int)
    return round(raw_num, 4)


def _extract_direction(text: str) -> Optional[str]:
    low = text.lower()
    if "↑" in text or re.search(r"\b(above|over|exceed|exceeds|exceeding|high|reach|higher|more\s+than|at\s+least|hike|increase)\b", low):
        if not re.search(r"\b(below|under|dip|low|lower|decrease|cut|drop)\b", low):
            return "above"
    if "↓" in text or re.search(r"\b(below|under|dip|low|lower|drop|fall|decrease|cut|less\s+than)\b", low):
        if not re.search(r"\b(above|over|hike|increase)\b", low):
            return "below"
    # More specific check when both words appear (e.g. "How low will Bitcoin get... below $55,000")
    if "↓" in text or re.search(r"\b(below|dip\s+to|how\s+low)\b", low):
        return "below"
    if "↑" in text or re.search(r"\b(above|reach|how\s+high)\b", low):
        return "above"
    return None


def _parse_fed_bucket(text: str) -> tuple[Optional[str], Optional[str]]:
    """Canonicalize Fed rate decision outcomes across Polymarket & Kalshi."""
    low = text.lower()
    if any(k in low for k in ("no change", "maintains", "maintain", "0 bps", "0bps", "hold", "unchanged")):
        return "hold_0", None
    if re.search(r"(cut|decrease).*(> ?25|50\+|\b50\b)", low) or re.search(r"(> ?25|50\+).*?(cut|decrease)", low):
        note = "Bracket nuance: Kalshi '>25bps' vs Polymarket '50+ bps' (equivalent for standard 25bps Fed steps)"
        return "cut_gt25", note
    if re.search(r"(hike|increase).*(> ?25|50\+|\b50\b)", low) or re.search(r"(> ?25|50\+).*?(hike|increase)", low):
        note = "Bracket nuance: Kalshi '>25bps' vs Polymarket '50+ bps' (equivalent for standard 25bps Fed steps)"
        return "hike_gt25", note
    if re.search(r"(cut|decrease).*25", low) or re.search(r"25\s*bps?\s*(cut|decrease)", low):
        return "cut_25", None
    if re.search(r"(hike|increase).*25", low) or re.search(r"25\s*bps?\s*(hike|increase)", low):
        return "hike_25", None
    return None, None


def extract_proposition(market: NormalizedMarket) -> CanonicalProposition:
    """Convert a NormalizedMarket into a structured CanonicalProposition for strict matching."""
    combined = f"{market.event_title} | {market.question} | {market.outcome_label}"
    low = combined.lower()
    ev_low = market.event_title.lower()
    q_low = market.question.lower()
    out_raw = market.outcome_label.strip()
    out_low = out_raw.lower()

    year, month = _extract_target_year_and_month(f"{market.event_title} {market.question} {out_raw}", market.end_date_iso)
    is_negated = bool(NEGATION_RE.search(market.question))

    # 1. Fed Rate Decision (December / January / etc.)
    if ("fed decision" in ev_low or "federal reserve" in q_low) and any(
        k in low for k in ("bps", "maintains rate", "no change", "cut", "hike", "decrease", "increase")
    ) and "how many" not in ev_low and "another fed" not in ev_low:
        bucket, note = _parse_fed_bucket(f"{out_raw} {market.question}")
        if bucket and month:
            nice = {
                "cut_gt25": "Cut >25bps / 50+ bps",
                "cut_25": "Cut 25bps",
                "hold_0": "No Change (0bps)",
                "hike_25": "Hike 25bps",
                "hike_gt25": "Hike >25bps / 50+ bps",
            }[bucket]
            return CanonicalProposition(
                market=market,
                family="fed_decision",
                entity_key=bucket,
                canonical_label=f"Fed Decision ({month.title()} {year or ''}) — {nice}".strip(),
                target_year=year,
                target_month=month,
                threshold_direction="below" if "cut" in bucket else ("above" if "hike" in bucket else "hold"),
                is_negated=False,
                rule_basis_note=note,
            )

    # 2. US Presidential Party Winner (must check before individual candidates)
    if (
        ("which party" in ev_low or "which political party" in ev_low or "(party)" in ev_low or "party" in out_low)
        and "president" in low
    ):
        party: Optional[str] = None
        check_str = out_low or q_low
        if "democrat" in check_str:
            party = "democratic"
        elif "republican" in check_str:
            party = "republican"
        if party and year:
            return CanonicalProposition(
                market=market,
                family="us_pres_party",
                entity_key=party,
                canonical_label=f"{year} US Presidential Election Party — {party.title()}",
                target_year=year,
                is_negated=is_negated,
            )

    # 3. Democratic Presidential Nominee
    if ("democratic" in low and "nominee" in low and "president" in low and "odds leader" not in ev_low):
        person = _normalize_person(out_raw) if out_raw else ""
        if person and person not in ("other", "someone else", "field") and not person.startswith("person "):
            return CanonicalProposition(
                market=market,
                family="us_pres_nom_dem",
                entity_key=person,
                canonical_label=f"{year or ''} Democratic Presidential Nominee — {person.title()}".strip(),
                target_year=year,
                is_negated=is_negated,
            )

    # 4. Republican Presidential Nominee
    if ("republican" in low and "nominee" in low and "president" in low and "odds leader" not in ev_low):
        person = _normalize_person(out_raw) if out_raw else ""
        if person and person not in ("other", "someone else", "field") and not person.startswith("person "):
            return CanonicalProposition(
                market=market,
                family="us_pres_nom_rep",
                entity_key=person,
                canonical_label=f"{year or ''} Republican Presidential Nominee — {person.title()}".strip(),
                target_year=year,
                is_negated=is_negated,
            )

    # 5. US Presidential Election Winner (Person)
    if (
        ("presidential election winner" in ev_low or "u.s. presidential election winner" in ev_low)
        and "party" not in ev_low
        and "french" not in ev_low
    ):
        person = _normalize_person(out_raw) if out_raw else ""
        if person and person not in ("other", "someone else", "field") and not person.startswith("person "):
            return CanonicalProposition(
                market=market,
                family="us_pres_winner",
                entity_key=person,
                canonical_label=f"{year or ''} US Presidential Winner — {person.title()}".strip(),
                target_year=year,
                is_negated=is_negated,
            )

    # 6. NBA Champion
    if ("nba" in low or "pro basketball champion" in ev_low) and ("champion" in low or "finals" in low):
        cand = _clean_text(out_raw)
        team = NBA_TEAM_ALIASES.get(cand)
        if team and year:
            return CanonicalProposition(
                market=market,
                family="nba_champ",
                entity_key=team,
                canonical_label=f"{year} NBA Champion — {team.title()}",
                target_year=year,
                is_negated=is_negated,
            )

    # 7. UEFA Champions League Winner
    if "champions league" in low and ("winner" in low or "champion" in low or "win the" in q_low):
        cand = _clean_text(out_raw)
        club = UCL_CLUB_ALIASES.get(cand, cand)
        if club and year:
            return CanonicalProposition(
                market=market,
                family="ucl_champ",
                entity_key=club,
                canonical_label=f"{year} UEFA Champions League Winner — {club.title()}",
                target_year=year,
                is_negated=is_negated,
            )

    # 8. Crypto Price Thresholds (Bitcoin / Ethereum)
    if any(c in low for c in ("bitcoin", "btc", "ethereum", "eth")) and (
        "price" in ev_low or "how high" in ev_low or "how low" in ev_low or "above" in low or "below" in low or "dip to" in low or "reach $" in low
    ):
        asset = "bitcoin" if ("bitcoin" in low or re.search(r"\bbtc\b", low)) else "ethereum"
        thresh = _extract_dollar_threshold(f"{out_raw} {market.question}")
        direction = _extract_direction(f"{out_raw} {market.question} {market.event_title}")
        if thresh is not None and direction and year:
            # Distinguish full-year touch ("year_end" or None) from daily/weekly contracts!
            m_tag = month if (month and month != "year_end") else "year_end"
            return CanonicalProposition(
                market=market,
                family="crypto_price",
                entity_key=f"{asset}_{direction}_{int(round(thresh))}",
                canonical_label=f"{asset.title()} {direction.title()} ${thresh:,.0f} ({year} {m_tag.replace('_', ' ')})",
                target_year=year,
                target_month=m_tag,
                threshold_value=thresh,
                threshold_direction=direction,
                is_negated=is_negated,
                rule_basis_note=(
                    "Check settlement feed & exact midnight ET cutoff (Kalshi CF Benchmarks vs Polymarket Binance/spot)."
                ),
            )

    # 9. Jensen Huang / NVIDIA CEO return
    if "jensen huang" in low and "nvidia" in low and any(k in low for k in ("rejoin", "return", "ceo")):
        m_tag = month or "year_end"
        return CanonicalProposition(
            market=market,
            family="exec_nvidia_jensen",
            entity_key="jensen_huang_nvidia_return",
            canonical_label=f"Jensen Huang Rejoins NVIDIA ({year or ''} {m_tag.replace('_', ' ')})".strip(),
            target_year=year,
            target_month=m_tag,
            is_negated=is_negated,
        )

    # 10. US Recession
    if "recession" in low and ("us" in low or "u.s." in low or "nber" in low or "this year" in low or "in 20" in low):
        if year:
            return CanonicalProposition(
                market=market,
                family="us_recession",
                entity_key="us_recession",
                canonical_label=f"US Recession in {year}",
                target_year=year,
                is_negated=is_negated,
            )

    # 11. General Binary Fallback (strict entity + numeric threshold + year + direction + polarity)
    clean_q = _clean_text(f"{market.question} {out_raw}")
    toks = frozenset(
        t for t in clean_q.split() if len(t) >= 3 and t not in STOPWORDS and not t.isdigit()
    )
    thresh = _extract_dollar_threshold(f"{market.question} {out_raw}")
    direction = _extract_direction(f"{market.question} {out_raw}")
    outcome_key = _normalize_person(out_raw) if out_raw else ""
    is_neg_combined = bool(NEGATION_RE.search(f"{market.event_title} {market.question}"))
    return CanonicalProposition(
        market=market,
        family="general_binary",
        entity_key="",
        canonical_label=market.question[:110],
        target_year=year,
        target_month=month,
        threshold_value=thresh,
        threshold_direction=direction,
        is_negated=is_neg_combined,
        outcome_key=outcome_key,
        tokens=toks,
    )


def propositions_compatible(a: CanonicalProposition, b: CanonicalProposition) -> tuple[bool, float]:
    """Return (is_match, confidence) enforcing hard rejection on year/month/threshold/direction/polarity."""
    if a.market.venue == b.market.venue:
        return False, 0.0

    # Hard Polarity Rejection: YES must mean YES on both sides.
    if a.is_negated != b.is_negated:
        return False, 0.0

    # Hard Year Rejection: mismatched target years NEVER match.
    if a.target_year is not None and b.target_year is not None and a.target_year != b.target_year:
        return False, 0.0
    if (a.target_year is None) != (b.target_year is None) and a.family != "general_binary":
        return False, 0.0

    # Hard Month / Date Precision Rejection.
    if a.target_month is not None and b.target_month is not None and a.target_month != b.target_month:
        return False, 0.0
    if a.family in ("fed_decision", "crypto_price", "exec_nvidia_jensen"):
        if a.target_month != b.target_month:
            return False, 0.0

    # Hard Numeric Threshold Rejection (tolerance <= $0.05 or 0.01% relative).
    if a.threshold_value is not None or b.threshold_value is not None:
        if a.threshold_value is None or b.threshold_value is None:
            return False, 0.0
        tol = max(0.05, 0.0001 * max(abs(a.threshold_value), abs(b.threshold_value)))
        if abs(a.threshold_value - b.threshold_value) > tol:
            return False, 0.0

    # Hard Direction Rejection.
    if a.threshold_direction is not None or b.threshold_direction is not None:
        if a.threshold_direction != b.threshold_direction:
            return False, 0.0

    # Structured family exact match.
    if a.family != "general_binary" and b.family != "general_binary":
        if a.family == b.family and a.entity_key and a.entity_key == b.entity_key:
            return True, 0.96
        return False, 0.0

    # General binary fallback: strict exact outcome_key (when multi-outcome legs), horizon check, and high token overlap.
    if a.family == "general_binary" and b.family == "general_binary":
        if a.target_year != b.target_year:
            return False, 0.0
        if a.outcome_key or b.outcome_key:
            if not (a.outcome_key and b.outcome_key and a.outcome_key == b.outcome_key):
                return False, 0.0
        # Reject general_binary matches whose end dates diverge by more than 180 days (different contract horizons).
        dt_a = _parse_iso_dt(a.market.end_date_iso)
        dt_b = _parse_iso_dt(b.market.end_date_iso)
        if dt_a is not None and dt_b is not None:
            if abs((dt_a - dt_b).total_seconds()) / 86400.0 > 180.0:
                return False, 0.0
        if not a.tokens or not b.tokens:
            return False, 0.0
        inter = a.tokens & b.tokens
        union = a.tokens | b.tokens
        if len(inter) < 4 or not union:
            return False, 0.0
        jaccard = len(inter) / len(union)
        if jaccard >= 0.58:
            return True, round(min(0.92, 0.5 + jaccard * 0.45), 3)

    return False, 0.0


def _parse_iso_dt(end_date_iso: Optional[str]) -> Optional[datetime]:
    if not end_date_iso:
        return None
    try:
        return datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_until(end_date_iso: Optional[str], now_utc: Optional[datetime] = None) -> Optional[float]:
    """Return remaining days until end_date_iso, or None if missing or already in the past."""
    dt = _parse_iso_dt(end_date_iso)
    if dt is None:
        return None
    ref = now_utc or datetime.now(timezone.utc)
    delta = (dt - ref).total_seconds() / 86400.0
    if delta <= 0.0:
        return None
    return round(delta, 1)


def _is_past_end_date(end_date_iso: Optional[str], now_utc: Optional[datetime] = None) -> bool:
    dt = _parse_iso_dt(end_date_iso)
    if dt is None:
        return False
    ref = now_utc or datetime.now(timezone.utc)
    return (dt - ref).total_seconds() <= 0.0


def evaluate_cross_venue_pair(
    prop_a: CanonicalProposition,
    prop_b: CanonicalProposition,
    confidence: float,
    slippage_bps: float = 0.0,
    min_divergence_pp: float = 3.0,
    now_utc: Optional[datetime] = None,
) -> Optional[CrossVenueMatch]:
    """Compute exact YES+NO arbitrage math, fee model, annualized ROI, and divergence metrics."""
    ma = prop_a.market
    mb = prop_b.market

    if ma.mid_prob is None or mb.mid_prob is None:
        return None

    # Canonical venue ordering: polymarket < kalshi < manifold
    venue_order = {"polymarket": 0, "kalshi": 1, "manifold": 2}
    if venue_order[mb.venue] < venue_order[ma.venue]:
        prop_a, prop_b = prop_b, prop_a
        ma, mb = mb, ma

    is_cash_eligible = ma.currency == "USD" and mb.currency == "USD"
    prob_a = float(ma.mid_prob)
    prob_b = float(mb.mid_prob)
    prob_diff_pp = round(abs(prob_a - prob_b) * 100.0, 2)

    best_dir = ""
    leg_a_action = ""
    leg_b_action = ""
    leg_a_price: Optional[float] = None
    leg_b_price: Optional[float] = None
    leg_a_sz: Optional[float] = None
    leg_b_sz: Optional[float] = None
    fee_a = 0.0
    fee_b = 0.0
    slip_tot = 0.0
    gross_cost: Optional[float] = None
    net_cost: Optional[float] = None
    gross_edge_pct: Optional[float] = None
    net_roi_pct: Optional[float] = None
    annualized_roi: Optional[float] = None

    days_left = _days_until(ma.end_date_iso, now_utc=now_utc) or _days_until(mb.end_date_iso, now_utc=now_utc)
    has_past_end = _is_past_end_date(ma.end_date_iso, now_utc=now_utc) or _is_past_end_date(
        mb.end_date_iso, now_utc=now_utc
    )

    if is_cash_eligible:
        candidates: list[tuple[float, dict]] = []

        # Direction 1: BUY YES on A + BUY NO on B
        if ma.yes_ask is not None and mb.no_ask is not None and ma.yes_ask > 0 and mb.no_ask > 0:
            fa = market_taker_fee(ma, ma.yes_ask)
            fb = market_taker_fee(mb, mb.no_ask)
            sl = slippage_cost(ma.yes_ask, slippage_bps) + slippage_cost(mb.no_ask, slippage_bps)
            gc = ma.yes_ask + mb.no_ask
            nc = gc + fa + fb + sl
            candidates.append(
                (
                    nc,
                    {
                        "dir": f"BUY YES ({ma.venue}) + BUY NO ({mb.venue})",
                        "a_act": "BUY YES",
                        "b_act": "BUY NO",
                        "a_p": ma.yes_ask,
                        "b_p": mb.no_ask,
                        "a_sz": ma.yes_ask_size,
                        "b_sz": mb.no_ask_size,
                        "fa": fa,
                        "fb": fb,
                        "sl": sl,
                        "gc": gc,
                        "nc": nc,
                    },
                )
            )

        # Direction 2: BUY NO on A + BUY YES on B
        if ma.no_ask is not None and mb.yes_ask is not None and ma.no_ask > 0 and mb.yes_ask > 0:
            fa = market_taker_fee(ma, ma.no_ask)
            fb = market_taker_fee(mb, mb.yes_ask)
            sl = slippage_cost(ma.no_ask, slippage_bps) + slippage_cost(mb.yes_ask, slippage_bps)
            gc = ma.no_ask + mb.yes_ask
            nc = gc + fa + fb + sl
            candidates.append(
                (
                    nc,
                    {
                        "dir": f"BUY NO ({ma.venue}) + BUY YES ({mb.venue})",
                        "a_act": "BUY NO",
                        "b_act": "BUY YES",
                        "a_p": ma.no_ask,
                        "b_p": mb.yes_ask,
                        "a_sz": ma.no_ask_size,
                        "b_sz": mb.yes_ask_size,
                        "fa": fa,
                        "fb": fb,
                        "sl": sl,
                        "gc": gc,
                        "nc": nc,
                    },
                )
            )

        if candidates:
            candidates.sort(key=lambda x: x[0])
            best = candidates[0][1]
            best_dir = best["dir"]
            leg_a_action = best["a_act"]
            leg_b_action = best["b_act"]
            leg_a_price = round(best["a_p"], 6)
            leg_b_price = round(best["b_p"], 6)
            leg_a_sz = round(best["a_sz"], 2) if best["a_sz"] else None
            leg_b_sz = round(best["b_sz"], 2) if best["b_sz"] else None
            fee_a = round(best["fa"], 6)
            fee_b = round(best["fb"], 6)
            slip_tot = round(best["sl"], 6)
            gross_cost = round(best["gc"], 6)
            net_cost = round(best["nc"], 6)
            gross_edge_pct = round((1.0 - gross_cost) * 100.0, 4)
            net_roi_pct = round(((1.0 - net_cost) / net_cost) * 100.0, 4) if net_cost > 0 else 0.0
            if days_left is not None and days_left > 0 and net_roi_pct > 0:
                annualized_roi = round(net_roi_pct * (365.0 / days_left), 2)

    risk_flags: list[str] = []
    if is_cash_eligible:
        risk_flags.append("LEGGED_IN_EXECUTION_RISK")
        risk_flags.append("RESOLUTION_ORACLE_BASIS_RISK")
        if has_past_end:
            risk_flags.append("PAST_SCHEDULED_END_DATE_PENDING_RESOLUTION")
        if days_left is not None and days_left > 180:
            risk_flags.append(f"CAPITAL_LOCK_{int(round(days_left))}D")
    else:
        risk_flags.append("PLAY_MONEY_BENCHMARK_NOT_CASH_ARB")

    if prop_a.rule_basis_note:
        risk_flags.append(prop_a.rule_basis_note)
    elif prop_b.rule_basis_note:
        risk_flags.append(prop_b.rule_basis_note)

    # Classify signal honestly
    signal_class: SignalClass
    if is_cash_eligible and net_cost is not None and net_cost < 1.0:
        signal_class = "PURE_CROSS_VENUE_ARB"
    elif is_cash_eligible and gross_cost is not None and gross_cost < 1.0:
        signal_class = "NEAR_ARB_MAKER_CANDIDATE"
    elif prob_diff_pp >= min_divergence_pp:
        signal_class = "HIGH_LIQ_DIVERGENCE"
    else:
        signal_class = "CROSS_VENUE_TRACKED"

    max_shares: Optional[float] = None
    max_usd: Optional[float] = None
    exp_profit: Optional[float] = None
    if leg_a_sz is not None and leg_b_sz is not None and net_cost is not None:
        max_shares = round(min(leg_a_sz, leg_b_sz), 2)
        max_usd = round(max_shares * net_cost, 2)
        exp_profit = round(max_shares * (1.0 - net_cost), 2)

    raw_id = f"{ma.venue}:{ma.market_id}|{mb.venue}:{mb.market_id}"
    match_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]

    return CrossVenueMatch(
        match_id=match_id,
        event_family=prop_a.family,
        canonical_label=prop_a.canonical_label,
        target_year=prop_a.target_year,
        target_month=prop_a.target_month,
        threshold_value=prop_a.threshold_value,
        threshold_direction=prop_a.threshold_direction,
        venue_a=ma.venue,
        venue_b=mb.venue,
        currency_a=ma.currency,
        currency_b=mb.currency,
        is_cash_arb_eligible=is_cash_eligible,
        market_a_id=ma.market_id,
        market_b_id=mb.market_id,
        title_a=f"{ma.event_title} — {ma.outcome_label}" if ma.outcome_label else ma.question,
        title_b=f"{mb.event_title} — {mb.outcome_label}" if mb.outcome_label else mb.question,
        url_a=ma.url,
        url_b=mb.url,
        end_date_iso=ma.end_date_iso or mb.end_date_iso,
        prob_a=round(prob_a, 4),
        prob_b=round(prob_b, 4),
        prob_diff_pp=prob_diff_pp,
        yes_ask_a=ma.yes_ask,
        no_ask_a=ma.no_ask,
        yes_ask_b=mb.yes_ask,
        no_ask_b=mb.no_ask,
        best_direction=best_dir,
        leg_a_action=leg_a_action,
        leg_b_action=leg_b_action,
        leg_a_price=leg_a_price,
        leg_b_price=leg_b_price,
        fee_a=fee_a,
        fee_b=fee_b,
        slippage_buffer=slip_tot,
        gross_cost=gross_cost,
        net_cost=net_cost,
        gross_edge_pct=gross_edge_pct,
        net_roi_pct=net_roi_pct,
        annualized_net_roi_pct=annualized_roi,
        days_to_expiry=days_left,
        leg_a_ask_size=leg_a_sz,
        leg_b_ask_size=leg_b_sz,
        max_fillable_shares=max_shares,
        max_fillable_usd=max_usd,
        expected_net_profit_usd=exp_profit,
        book_verified=(leg_a_sz is not None and leg_b_sz is not None),
        volume_24h_a=round(ma.volume_24h, 2),
        volume_24h_b=round(mb.volume_24h, 2),
        liquidity_a=round(ma.liquidity, 2),
        liquidity_b=round(mb.liquidity, 2),
        signal_class=signal_class,
        match_confidence=confidence,
        risk_flags=risk_flags,
    )


def match_cross_venue(
    markets: list[NormalizedMarket],
    slippage_bps: float = 0.0,
    min_divergence_pp: float = 3.0,
    min_activity_threshold: float = 200.0,
    now_utc: Optional[datetime] = None,
) -> list[CrossVenueMatch]:
    """Match equivalent propositions across Polymarket, Kalshi, and Manifold."""
    props = [extract_proposition(m) for m in markets if m.mid_prob is not None]

    # Bucket structured families by (family, entity_key) for fast O(N) matching!
    by_bucket: dict[tuple[str, str], list[CanonicalProposition]] = {}
    general_props: list[CanonicalProposition] = []

    for p in props:
        if p.family != "general_binary" and p.entity_key:
            by_bucket.setdefault((p.family, p.entity_key), []).append(p)
        else:
            # Filter out tiny zero-activity markets before O(N^2) general matching.
            activity = max(p.market.volume_24h, p.market.volume_total, p.market.liquidity)
            if activity >= min_activity_threshold:
                general_props.append(p)

    seen_pairs: set[tuple[str, str]] = set()
    matches: list[CrossVenueMatch] = []

    def _try_pair(pa: CanonicalProposition, pb: CanonicalProposition) -> None:
        key = tuple(sorted((f"{pa.market.venue}:{pa.market.market_id}", f"{pb.market.venue}:{pb.market.market_id}")))
        if key in seen_pairs:
            return
        ok, conf = propositions_compatible(pa, pb)
        if not ok:
            return
        seen_pairs.add(key)

        # Require meaningful liquidity/volume on both sides for non-arb divergence signals.
        act_a = max(pa.market.volume_24h, pa.market.volume_total, pa.market.liquidity)
        act_b = max(pb.market.volume_24h, pb.market.volume_total, pb.market.liquidity)
        if act_a < min_activity_threshold or act_b < min_activity_threshold:
            return

        res = evaluate_cross_venue_pair(
            pa,
            pb,
            confidence=conf,
            slippage_bps=slippage_bps,
            min_divergence_pp=min_divergence_pp,
            now_utc=now_utc,
        )
        if res is not None:
            matches.append(res)

    for group in by_bucket.values():
        n = len(group)
        for i in range(n):
            for j in range(i + 1, n):
                _try_pair(group[i], group[j])

    n_gen = len(general_props)
    for i in range(n_gen):
        for j in range(i + 1, n_gen):
            if general_props[i].market.venue != general_props[j].market.venue:
                _try_pair(general_props[i], general_props[j])

    rank = {
        "PURE_CROSS_VENUE_ARB": 0,
        "NEAR_ARB_MAKER_CANDIDATE": 1,
        "HIGH_LIQ_DIVERGENCE": 2,
        "CROSS_VENUE_TRACKED": 3,
    }
    matches.sort(
        key=lambda m: (
            rank.get(m.signal_class, 9),
            -(m.net_roi_pct if m.net_roi_pct is not None else -999.0),
            -m.prob_diff_pp,
        )
    )
    return matches


def enrich_cross_venue_orderbooks(
    client: httpx.Client,
    matches: list[CrossVenueMatch],
    markets_by_key: dict[tuple[str, str], NormalizedMarket],
    slippage_bps: float = 0.0,
    max_matches_to_enrich: int = 12,
) -> int:
    """Enrich top cash-arb & near-arb cross-venue matches with live CLOB + Kalshi orderbook depth."""
    books_fetched = 0
    enriched = 0

    for m in matches:
        if enriched >= max_matches_to_enrich:
            break
        if not m.is_cash_arb_eligible:
            continue
        if m.signal_class not in ("PURE_CROSS_VENUE_ARB", "NEAR_ARB_MAKER_CANDIDATE") and m.prob_diff_pp < 2.0:
            continue

        ma = markets_by_key.get((m.venue_a, m.market_a_id))
        mb = markets_by_key.get((m.venue_b, m.market_b_id))
        if ma is None or mb is None:
            continue

        # Refresh Polymarket leg live from CLOB /book
        if ma.venue == "polymarket":
            tid = ma.yes_token_id if m.leg_a_action == "BUY YES" else ma.no_token_id
            if tid:
                snap = fetch_polymarket_clob_book(client, token_id=tid, market_id=ma.market_id)
                books_fetched += 1
                if snap.error is None and snap.best_ask is not None and snap.best_ask_size is not None:
                    m.leg_a_price = round(snap.best_ask, 6)
                    m.leg_a_ask_size = round(snap.best_ask_size, 2)
                    m.fee_a = round(market_taker_fee(ma, m.leg_a_price), 6)

        # Refresh Kalshi leg live from /markets/{ticker}/orderbook if size wasn't already populated
        if mb.venue == "kalshi":
            side = "yes" if m.leg_b_action == "BUY YES" else "no"
            snap_k = fetch_kalshi_orderbook(client, ticker=mb.market_id, side=side)
            books_fetched += 1
            if snap_k.error is None and snap_k.best_ask is not None and snap_k.best_ask_size is not None:
                m.leg_b_price = round(snap_k.best_ask, 6)
                m.leg_b_ask_size = round(snap_k.best_ask_size, 2)
                m.fee_b = round(market_taker_fee(mb, m.leg_b_price), 6)

        # Recompute exact totals with live book prices & depth!
        if m.leg_a_price is not None and m.leg_b_price is not None:
            m.slippage_buffer = round(
                slippage_cost(m.leg_a_price, slippage_bps) + slippage_cost(m.leg_b_price, slippage_bps),
                6,
            )
            m.gross_cost = round(m.leg_a_price + m.leg_b_price, 6)
            m.net_cost = round(m.gross_cost + m.fee_a + m.fee_b + m.slippage_buffer, 6)
            m.gross_edge_pct = round((1.0 - m.gross_cost) * 100.0, 4)
            m.net_roi_pct = (
                round(((1.0 - m.net_cost) / m.net_cost) * 100.0, 4) if m.net_cost > 0 else 0.0
            )
            if m.days_to_expiry and m.days_to_expiry > 0 and m.net_roi_pct > 0:
                m.annualized_net_roi_pct = round(m.net_roi_pct * (365.0 / m.days_to_expiry), 2)
            else:
                m.annualized_net_roi_pct = None

            if m.net_cost < 1.0:
                m.signal_class = "PURE_CROSS_VENUE_ARB"
            elif m.gross_cost < 1.0:
                m.signal_class = "NEAR_ARB_MAKER_CANDIDATE"
            elif m.prob_diff_pp >= 3.0:
                m.signal_class = "HIGH_LIQ_DIVERGENCE"
            else:
                m.signal_class = "CROSS_VENUE_TRACKED"

            if m.leg_a_ask_size is not None and m.leg_b_ask_size is not None:
                m.book_verified = True
                m.max_fillable_shares = round(min(m.leg_a_ask_size, m.leg_b_ask_size), 2)
                m.max_fillable_usd = round(m.max_fillable_shares * m.net_cost, 2)
                m.expected_net_profit_usd = round(m.max_fillable_shares * (1.0 - m.net_cost), 2)

        enriched += 1

    # Re-sort after live orderbook enrichment
    rank = {
        "PURE_CROSS_VENUE_ARB": 0,
        "NEAR_ARB_MAKER_CANDIDATE": 1,
        "HIGH_LIQ_DIVERGENCE": 2,
        "CROSS_VENUE_TRACKED": 3,
    }
    matches.sort(
        key=lambda x: (
            rank.get(x.signal_class, 9),
            -(x.net_roi_pct if x.net_roi_pct is not None else -999.0),
            -x.prob_diff_pp,
        )
    )
    return books_fetched
