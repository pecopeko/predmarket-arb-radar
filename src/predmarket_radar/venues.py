"""Polite, read-only public API fetchers for Polymarket, Kalshi, and Manifold Markets.

Strict zero-key, zero-custody, read-only architecture. Never fabricates missing
prices or swallows HTTP errors — every venue response and status code is recorded
in VenueStatus provenance records.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .models import (
    MultiOutcomeEvent,
    NormalizedMarket,
    OrderBookLevel,
    OrderBookSnapshot,
    VenueStatus,
)

USER_AGENT = "predmarket-arb-radar/1.0 (+https://github.com/pecopeko/predmarket-arb-radar)"

POLYMARKET_GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
POLYMARKET_CLOB_BOOK_URL = "https://clob.polymarket.com/book"
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MANIFOLD_BASE_URL = "https://api.manifold.markets/v0"

# High-liquidity flagship Kalshi series that frequently overlap with Polymarket & Manifold.
DEFAULT_KALSHI_SERIES: tuple[str, ...] = (
    "KXFEDDECISION",
    "KXPRESNOMD",
    "KXPRESNOMR",
    "KXPRESPERSON",
    "KXPRESPARTY",
    "KXNBA",
    "KXSB",
    "KXNHL",
    "KXMLB",
    "KXUCL",
    "KXCPI",
    "KXGDP",
    "KXRECSSNBER",
    "KXBTCMAXY",
    "KXBTCMINY",
    "KXETHMAXY",
    "KXIPO",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN check
        return None
    return out


def _clamp_prob(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    if val < 0.0 or val > 1.0:
        return None
    return round(val, 6)


def _parse_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            return []
    return []


def _get_with_retry(
    client: httpx.Client,
    url: str,
    params: Optional[dict[str, Any]] = None,
    max_retries: int = 2,
    base_delay: float = 0.35,
) -> tuple[Optional[httpx.Response], Optional[str], float]:
    """Perform a polite GET with bounded 429/5xx retry; never fabricates responses."""
    t0 = time.monotonic()
    last_err: Optional[str] = None
    last_resp: Optional[httpx.Response] = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.get(url, params=params)
            last_resp = resp
            if resp.status_code == 200:
                return resp, None, round((time.monotonic() - t0) * 1000.0, 1)
            if resp.status_code in (429, 502, 503, 504) and attempt < max_retries:
                time.sleep(base_delay * (2**attempt))
                continue
            body_preview = resp.text[:180].strip().replace("\n", " ")
            last_err = f"HTTP {resp.status_code}: {body_preview}"
            break
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                time.sleep(base_delay * (2**attempt))
                continue
            break
    return last_resp, last_err, round((time.monotonic() - t0) * 1000.0, 1)


def parse_polymarket_clob_book(
    payload: dict[str, Any],
    market_id: str = "",
    token_id: str = "",
    http_status: int = 200,
) -> OrderBookSnapshot:
    """Parse Polymarket CLOB /book payload with EXPLICIT sorting.

    Polymarket CLOB returns asks sorted descending (0.999 first, best ask last)
    and bids sorted ascending (0.001 first, best bid last). We always sort asks
    ascending and bids descending so index 0 is guaranteed to be top-of-book.
    """
    raw_bids = payload.get("bids") or []
    raw_asks = payload.get("asks") or []

    bids: list[OrderBookLevel] = []
    for lvl in raw_bids:
        p = _clamp_prob(_safe_float(lvl.get("price") if isinstance(lvl, dict) else None))
        s = _safe_float(lvl.get("size") if isinstance(lvl, dict) else None)
        if p is not None and s is not None and s > 0:
            bids.append(OrderBookLevel(price=p, size=s))

    asks: list[OrderBookLevel] = []
    for lvl in raw_asks:
        p = _clamp_prob(_safe_float(lvl.get("price") if isinstance(lvl, dict) else None))
        s = _safe_float(lvl.get("size") if isinstance(lvl, dict) else None)
        if p is not None and s is not None and s > 0:
            asks.append(OrderBookLevel(price=p, size=s))

    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)

    return OrderBookSnapshot(
        venue="polymarket",
        market_id=market_id,
        token_id=token_id or str(payload.get("asset_id") or ""),
        best_bid=bids[0].price if bids else None,
        best_bid_size=bids[0].size if bids else None,
        best_ask=asks[0].price if asks else None,
        best_ask_size=asks[0].size if asks else None,
        bids=bids[:10],
        asks=asks[:10],
        source=POLYMARKET_CLOB_BOOK_URL,
        http_status=http_status,
    )


def parse_kalshi_orderbook(
    payload: dict[str, Any],
    ticker: str,
    side: str = "yes",
    http_status: int = 200,
) -> OrderBookSnapshot:
    """Parse Kalshi /markets/{ticker}/orderbook payload.

    Kalshi orderbooks list YES bids (`yes_dollars`) and NO bids (`no_dollars`).
    Because YES + NO = $1.00, the best YES ask equals `1.00 - max(no_bid)` with
    fillable size equal to the size resting at `max(no_bid)`, and vice versa.
    """
    ob = payload.get("orderbook_fp") or payload.get("orderbook") or {}
    yes_raw = ob.get("yes_dollars") or ob.get("yes") or []
    no_raw = ob.get("no_dollars") or ob.get("no") or []
    is_cents = "yes_dollars" not in ob and "no_dollars" not in ob

    def _parse_bids(rows: list[Any]) -> list[OrderBookLevel]:
        out: list[OrderBookLevel] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            p = _safe_float(row[0])
            s = _safe_float(row[1])
            if p is None or s is None or s <= 0:
                continue
            if is_cents and p > 1.0:
                p = p / 100.0
            cp = _clamp_prob(p)
            if cp is not None:
                out.append(OrderBookLevel(price=cp, size=s))
        out.sort(key=lambda x: x.price, reverse=True)
        return out

    yes_bids = _parse_bids(yes_raw)
    no_bids = _parse_bids(no_raw)

    if side.lower() == "no":
        bids = no_bids
        asks = [
            OrderBookLevel(price=round(1.0 - lvl.price, 6), size=lvl.size)
            for lvl in yes_bids
            if 0.0 <= round(1.0 - lvl.price, 6) <= 1.0
        ]
    else:
        bids = yes_bids
        asks = [
            OrderBookLevel(price=round(1.0 - lvl.price, 6), size=lvl.size)
            for lvl in no_bids
            if 0.0 <= round(1.0 - lvl.price, 6) <= 1.0
        ]
    asks.sort(key=lambda x: x.price)

    return OrderBookSnapshot(
        venue="kalshi",
        market_id=ticker,
        token_id=f"{ticker}:{side.upper()}",
        best_bid=bids[0].price if bids else None,
        best_bid_size=bids[0].size if bids else None,
        best_ask=asks[0].price if asks else None,
        best_ask_size=asks[0].size if asks else None,
        bids=bids[:10],
        asks=asks[:10],
        source=f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook",
        http_status=http_status,
    )


def fetch_polymarket_clob_book(
    client: httpx.Client,
    token_id: str,
    market_id: str = "",
) -> OrderBookSnapshot:
    """Fetch a live Polymarket CLOB order book snapshot for a specific token ID."""
    resp, err, _ = _get_with_retry(
        client,
        POLYMARKET_CLOB_BOOK_URL,
        params={"token_id": token_id},
        max_retries=1,
    )
    if err or resp is None:
        return OrderBookSnapshot(
            venue="polymarket",
            market_id=market_id,
            token_id=token_id,
            source=POLYMARKET_CLOB_BOOK_URL,
            http_status=resp.status_code if resp is not None else None,
            error=err or "Unknown CLOB error",
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        return OrderBookSnapshot(
            venue="polymarket",
            market_id=market_id,
            token_id=token_id,
            source=POLYMARKET_CLOB_BOOK_URL,
            http_status=resp.status_code,
            error=f"Invalid JSON: {exc}",
        )
    return parse_polymarket_clob_book(
        payload=payload,
        market_id=market_id,
        token_id=token_id,
        http_status=resp.status_code,
    )


def fetch_kalshi_orderbook(
    client: httpx.Client,
    ticker: str,
    side: str = "yes",
) -> OrderBookSnapshot:
    """Fetch a live Kalshi public order book snapshot for a ticker."""
    url = f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook"
    resp, err, _ = _get_with_retry(client, url, max_retries=1)
    if err or resp is None:
        return OrderBookSnapshot(
            venue="kalshi",
            market_id=ticker,
            token_id=f"{ticker}:{side.upper()}",
            source=url,
            http_status=resp.status_code if resp is not None else None,
            error=err or "Unknown Kalshi orderbook error",
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        return OrderBookSnapshot(
            venue="kalshi",
            market_id=ticker,
            token_id=f"{ticker}:{side.upper()}",
            source=url,
            http_status=resp.status_code,
            error=f"Invalid JSON: {exc}",
        )
    return parse_kalshi_orderbook(
        payload=payload,
        ticker=ticker,
        side=side,
        http_status=resp.status_code,
    )


def _normalize_polymarket_event(
    ev: dict[str, Any],
) -> tuple[list[NormalizedMarket], Optional[MultiOutcomeEvent]]:
    """Convert one Polymarket Gamma event dict into normalized markets + optional multi-outcome basket."""
    event_id = str(ev.get("id") or "")
    slug = str(ev.get("slug") or "")
    title = str(ev.get("title") or "").strip()
    url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
    neg_risk = bool(ev.get("negRisk") or ev.get("enableNegRisk"))
    neg_risk_augmented = bool(ev.get("negRiskAugmented"))
    event_end = ev.get("endDate")
    event_vol24 = _safe_float(ev.get("volume24hr")) or 0.0
    event_liq = _safe_float(ev.get("liquidity")) or 0.0

    raw_markets = ev.get("markets") or []
    total_count = len(raw_markets)
    closed_count = 0
    inactive_count = 0
    has_excluded_other = False
    has_tradable_other = False
    active_legs: list[NormalizedMarket] = []

    for m in raw_markets:
        is_closed = bool(m.get("closed"))
        is_active = bool(m.get("active"))
        is_accepting = bool(m.get("acceptingOrders", True))
        is_other = bool(m.get("negRiskOther")) or (
            str(m.get("groupItemTitle") or "").strip().lower() == "other"
        )
        if is_closed:
            closed_count += 1
            if is_other:
                has_excluded_other = True
            continue
        if not is_active or not is_accepting:
            inactive_count += 1
            if is_other:
                has_excluded_other = True
            continue

        if is_other:
            has_tradable_other = True

        outcomes = _parse_json_list(m.get("outcomes"))
        outcome_prices = _parse_json_list(m.get("outcomePrices"))
        token_ids = _parse_json_list(m.get("clobTokenIds"))

        # Only normalize binary Yes/No contracts (multi-outcome events are arrays of Yes/No legs).
        if outcomes and len(outcomes) == 2:
            o0 = str(outcomes[0]).strip().lower()
            o1 = str(outcomes[1]).strip().lower()
            if not (o0 == "yes" and o1 == "no"):
                # Sports head-to-head moneyline markets have team names as outcomes; skip for Yes/No arb unless binary Yes/No.
                continue

        yes_bid = _clamp_prob(_safe_float(m.get("bestBid")))
        yes_ask = _clamp_prob(_safe_float(m.get("bestAsk")))
        p0 = _clamp_prob(_safe_float(outcome_prices[0])) if outcome_prices else None

        # Derive NO ask/bid cleanly from binary complementarity (1 - YES bid/ask).
        no_ask = _clamp_prob(1.0 - yes_bid) if (yes_bid is not None and yes_bid > 0.0) else None
        no_bid = _clamp_prob(1.0 - yes_ask) if (yes_ask is not None and yes_ask < 1.0) else None

        if yes_bid is not None and yes_ask is not None:
            mid_prob = round((yes_bid + yes_ask) / 2.0, 6)
        elif p0 is not None:
            mid_prob = p0
        elif yes_ask is not None:
            mid_prob = yes_ask
        else:
            mid_prob = None

        fee_schedule = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
        fees_enabled = bool(m.get("feesEnabled"))
        taker_rate = _safe_float(fee_schedule.get("rate")) if fees_enabled else 0.0
        if taker_rate is None:
            taker_rate = 0.0
        fee_type = str(m.get("feeType") or ("polymarket_taker" if fees_enabled else "fee_free"))

        yes_token_id = str(token_ids[0]) if len(token_ids) >= 1 else None
        no_token_id = str(token_ids[1]) if len(token_ids) >= 2 else None

        question = str(m.get("question") or title).strip()
        group_label = str(m.get("groupItemTitle") or "").strip()

        leg = NormalizedMarket(
            venue="polymarket",
            currency="USD",
            market_id=str(m.get("id") or ""),
            event_id=event_id,
            event_title=title,
            question=question,
            outcome_label=group_label,
            category=fee_type,
            url=url,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            mid_prob=mid_prob,
            volume_24h=_safe_float(m.get("volume24hr")) or 0.0,
            volume_total=_safe_float(m.get("volumeNum")) or _safe_float(m.get("volume")) or 0.0,
            liquidity=_safe_float(m.get("liquidityNum")) or _safe_float(m.get("liquidity")) or 0.0,
            fees_enabled=fees_enabled,
            taker_fee_rate=taker_rate,
            fee_model=f"rate*p*(1-p) [rate={taker_rate:.4f}]" if fees_enabled else "0.0 (fee-free)",
            end_date_iso=str(m.get("endDate") or event_end or "") or None,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            is_mutually_exclusive_leg=neg_risk,
            is_neg_risk_other=is_other,
            rules_summary=str(m.get("description") or "")[:300],
        )
        active_legs.append(leg)

    multi_ev: Optional[MultiOutcomeEvent] = None
    if len(active_legs) >= 2 and neg_risk:
        multi_ev = MultiOutcomeEvent(
            venue="polymarket",
            currency="USD",
            event_id=event_id,
            slug=slug,
            title=title,
            category=active_legs[0].category if active_legs else "",
            url=url,
            end_date_iso=str(event_end or "") or None,
            is_mutually_exclusive=neg_risk,
            neg_risk=neg_risk,
            neg_risk_augmented=neg_risk_augmented,
            total_markets_in_event=total_count,
            active_markets_count=len(active_legs),
            closed_markets_count=closed_count,
            inactive_placeholder_count=inactive_count,
            has_excluded_other_leg=has_excluded_other,
            has_tradable_other_leg=has_tradable_other,
            volume_24h=event_vol24,
            liquidity=event_liq,
            legs=active_legs,
        )

    return active_legs, multi_ev


def fetch_polymarket_data(
    client: httpx.Client,
    max_events: int = 200,
    include_top_volume_page: bool = True,
) -> tuple[list[NormalizedMarket], list[MultiOutcomeEvent], VenueStatus]:
    """Fetch live active Polymarket events from Gamma API."""
    t0 = time.monotonic()
    fetched_at = utc_now_iso()
    raw_by_id: dict[str, dict[str, Any]] = {}
    last_http: Optional[int] = None
    errors: list[str] = []

    pages = max(1, (max_events + 99) // 100)
    for page in range(pages):
        limit = min(100, max_events - page * 100)
        if limit <= 0:
            break
        resp, err, _ = _get_with_retry(
            client,
            POLYMARKET_GAMMA_EVENTS_URL,
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": page * 100,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        if resp is not None:
            last_http = resp.status_code
        if err or resp is None:
            errors.append(err or "Polymarket request failed")
            break
        try:
            batch = resp.json()
        except ValueError as exc:
            errors.append(f"Invalid JSON: {exc}")
            break
        if not isinstance(batch, list):
            errors.append(f"Unexpected payload type: {type(batch).__name__}")
            break
        for ev in batch:
            eid = str(ev.get("id") or "")
            if eid:
                raw_by_id[eid] = ev
        if len(batch) < limit:
            break

    if include_top_volume_page and not errors:
        resp, err, _ = _get_with_retry(
            client,
            POLYMARKET_GAMMA_EVENTS_URL,
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "offset": 0,
                "order": "volume",
                "ascending": "false",
            },
        )
        if resp is not None and err is None:
            try:
                batch = resp.json()
                if isinstance(batch, list):
                    for ev in batch:
                        eid = str(ev.get("id") or "")
                        if eid and eid not in raw_by_id:
                            raw_by_id[eid] = ev
            except ValueError:
                pass

    markets: list[NormalizedMarket] = []
    multi_events: list[MultiOutcomeEvent] = []
    for ev in raw_by_id.values():
        m_list, m_ev = _normalize_polymarket_event(ev)
        markets.extend(m_list)
        if m_ev is not None:
            multi_events.append(m_ev)

    status = VenueStatus(
        venue="polymarket",
        endpoint=POLYMARKET_GAMMA_EVENTS_URL,
        ok=len(raw_by_id) > 0 and not errors,
        http_status=last_http,
        latency_ms=round((time.monotonic() - t0) * 1000.0, 1),
        events_fetched=len(raw_by_id),
        markets_fetched=len(markets),
        currency="USD",
        error="; ".join(errors) if errors else None,
        fetched_at_utc=fetched_at,
    )
    return markets, multi_events, status


def _kalshi_price(m: dict[str, Any], dollar_key: str, cent_key: str) -> Optional[float]:
    val = _safe_float(m.get(dollar_key))
    if val is not None:
        return _clamp_prob(val)
    cval = _safe_float(m.get(cent_key))
    if cval is not None:
        return _clamp_prob(cval / 100.0 if cval > 1.0 else cval)
    return None


def _normalize_kalshi_event(
    ev: dict[str, Any],
) -> tuple[list[NormalizedMarket], Optional[MultiOutcomeEvent]]:
    """Convert one Kalshi v2 event (with nested markets) into normalized objects."""
    event_ticker = str(ev.get("event_ticker") or "")
    series_ticker = str(ev.get("series_ticker") or "")
    title = str(ev.get("title") or "").strip()
    category = str(ev.get("category") or "")
    is_mutex = bool(ev.get("mutually_exclusive"))
    series_slug = series_ticker.lower() if series_ticker else event_ticker.lower()
    url = f"https://kalshi.com/markets/{series_slug}"

    raw_markets = ev.get("markets") or []
    active_legs: list[NormalizedMarket] = []
    total_v24 = 0.0
    total_oi = 0.0
    has_tradable_other = False
    end_date_iso: Optional[str] = None

    for m in raw_markets:
        status = str(m.get("status") or "").lower()
        if status and status not in ("active", "open"):
            continue
        if bool(m.get("is_provisional")) or m.get("mve_collection_ticker"):
            continue

        ticker = str(m.get("ticker") or "")
        yes_ask = _kalshi_price(m, "yes_ask_dollars", "yes_ask")
        yes_bid = _kalshi_price(m, "yes_bid_dollars", "yes_bid")
        no_ask = _kalshi_price(m, "no_ask_dollars", "no_ask")
        no_bid = _kalshi_price(m, "no_bid_dollars", "no_bid")
        last_p = _kalshi_price(m, "last_price_dollars", "last_price")

        # Kalshi returns 0.00 / 1.00 when one side of the book is empty; keep honest None for empty 0 asks.
        if yes_ask is not None and yes_ask <= 0.0:
            yes_ask = None
        if no_ask is not None and no_ask <= 0.0:
            no_ask = None

        if yes_bid is not None and yes_ask is not None:
            mid_prob = round((yes_bid + yes_ask) / 2.0, 6)
        elif last_p is not None and last_p > 0.0:
            mid_prob = last_p
        elif yes_ask is not None:
            mid_prob = yes_ask
        else:
            mid_prob = None

        yes_ask_size = _safe_float(m.get("yes_ask_size_fp"))
        yes_bid_size = _safe_float(m.get("yes_bid_size_fp"))
        # On Kalshi binary contracts, buying NO at no_ask hits YES bids at yes_bid!
        no_ask_size = yes_bid_size if (yes_bid_size is not None and yes_bid_size > 0) else None

        v24 = _safe_float(m.get("volume_24h_fp")) or _safe_float(m.get("volume_24h")) or 0.0
        vtot = _safe_float(m.get("volume_fp")) or _safe_float(m.get("volume")) or 0.0
        oi = _safe_float(m.get("open_interest_fp")) or _safe_float(m.get("open_interest")) or 0.0
        liq = _safe_float(m.get("liquidity_dollars")) or oi
        total_v24 += v24
        total_oi += oi

        sub = str(m.get("yes_sub_title") or "").strip()
        if sub.lower() in ("other", "someone else", "any other", "field"):
            has_tradable_other = True
        m_title = str(m.get("title") or title).strip()
        close_t = str(m.get("close_time") or m.get("expected_expiration_time") or "") or None
        if end_date_iso is None and close_t:
            end_date_iso = close_t

        leg = NormalizedMarket(
            venue="kalshi",
            currency="USD",
            market_id=ticker,
            event_id=event_ticker,
            event_title=title,
            question=m_title,
            outcome_label=sub,
            category=category,
            url=url,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            mid_prob=mid_prob,
            yes_ask_size=yes_ask_size if (yes_ask_size and yes_ask_size > 0) else None,
            no_ask_size=no_ask_size,
            yes_bid_size=yes_bid_size if (yes_bid_size and yes_bid_size > 0) else None,
            volume_24h=v24,
            volume_total=vtot,
            liquidity=liq,
            open_interest=oi,
            fees_enabled=True,
            taker_fee_rate=0.07,
            fee_model="ceil_cent(0.07*C*P*(1-P))",
            end_date_iso=close_t,
            is_mutually_exclusive_leg=is_mutex,
            rules_summary=str(m.get("rules_primary") or "")[:300],
        )
        active_legs.append(leg)

    multi_ev: Optional[MultiOutcomeEvent] = None
    if len(active_legs) >= 2 and is_mutex:
        multi_ev = MultiOutcomeEvent(
            venue="kalshi",
            currency="USD",
            event_id=event_ticker,
            slug=event_ticker.lower(),
            title=title,
            category=category,
            url=url,
            end_date_iso=end_date_iso,
            is_mutually_exclusive=is_mutex,
            neg_risk=False,
            neg_risk_augmented=False,
            total_markets_in_event=len(raw_markets),
            active_markets_count=len(active_legs),
            closed_markets_count=max(0, len(raw_markets) - len(active_legs)),
            inactive_placeholder_count=0,
            has_excluded_other_leg=False,
            has_tradable_other_leg=has_tradable_other,
            volume_24h=total_v24,
            liquidity=total_oi,
            legs=active_legs,
        )

    return active_legs, multi_ev


def fetch_kalshi_data(
    client: httpx.Client,
    series_tickers: tuple[str, ...] = DEFAULT_KALSHI_SERIES,
    general_pages: int = 2,
) -> tuple[list[NormalizedMarket], list[MultiOutcomeEvent], VenueStatus]:
    """Fetch live open Kalshi events across flagship series + general open pages.

    Gracefully degrades if Kalshi rate-limits or auth-gates any request, recording
    the exact HTTP status and error honestly.
    """
    t0 = time.monotonic()
    fetched_at = utc_now_iso()
    events_url = f"{KALSHI_BASE_URL}/events"
    raw_by_ticker: dict[str, dict[str, Any]] = {}
    last_http: Optional[int] = None
    errors: list[str] = []
    auth_blocked = False

    # 1. Fetch flagship cross-venue series first (fast & targeted).
    for st in series_tickers:
        resp, err, _ = _get_with_retry(
            client,
            events_url,
            params={
                "status": "open",
                "with_nested_markets": "true",
                "series_ticker": st,
                "limit": 5,
            },
            max_retries=1,
        )
        if resp is not None:
            last_http = resp.status_code
            if resp.status_code in (401, 403):
                auth_blocked = True
                errors.append(f"Auth/access gated on {st}: HTTP {resp.status_code}")
                break
        if err or resp is None:
            errors.append(f"{st}: {err}")
            continue
        try:
            payload = resp.json()
        except ValueError as exc:
            errors.append(f"{st}: Invalid JSON ({exc})")
            continue
        for ev in payload.get("events") or []:
            et = str(ev.get("event_ticker") or "")
            if et:
                raw_by_ticker[et] = ev
        time.sleep(0.05)

    # 2. Also fetch general open events pages to catch broad/newly listed Kalshi markets.
    if not auth_blocked and general_pages > 0:
        cursor: Optional[str] = None
        for _ in range(general_pages):
            params: dict[str, Any] = {
                "status": "open",
                "with_nested_markets": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            resp, err, _ = _get_with_retry(client, events_url, params=params, max_retries=1)
            if resp is not None:
                last_http = resp.status_code
            if err or resp is None:
                errors.append(f"general_events: {err}")
                break
            try:
                payload = resp.json()
            except ValueError:
                break
            evs = payload.get("events") or []
            for ev in evs:
                et = str(ev.get("event_ticker") or "")
                if et and et not in raw_by_ticker:
                    raw_by_ticker[et] = ev
            cursor = payload.get("cursor")
            if not cursor or not evs:
                break
            time.sleep(0.05)

    markets: list[NormalizedMarket] = []
    multi_events: list[MultiOutcomeEvent] = []
    for ev in raw_by_ticker.values():
        m_list, m_ev = _normalize_kalshi_event(ev)
        markets.extend(m_list)
        if m_ev is not None:
            multi_events.append(m_ev)

    status = VenueStatus(
        venue="kalshi",
        endpoint=events_url,
        ok=len(raw_by_ticker) > 0 and not auth_blocked,
        http_status=last_http,
        latency_ms=round((time.monotonic() - t0) * 1000.0, 1),
        events_fetched=len(raw_by_ticker),
        markets_fetched=len(markets),
        currency="USD",
        error="; ".join(errors[:4]) if errors else None,
        fetched_at_utc=fetched_at,
    )
    return markets, multi_events, status


def _epoch_ms_to_iso(ms: Any) -> Optional[str]:
    val = _safe_float(ms)
    if val is None or val <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def fetch_manifold_data(
    client: httpx.Client,
    limit_per_sort: int = 80,
    expand_top_multichoice: int = 10,
) -> tuple[list[NormalizedMarket], VenueStatus]:
    """Fetch open Manifold Markets (play-money Mana benchmark for divergence radar)."""
    t0 = time.monotonic()
    fetched_at = utc_now_iso()
    search_url = f"{MANIFOLD_BASE_URL}/search-markets"
    raw_by_id: dict[str, dict[str, Any]] = {}
    last_http: Optional[int] = None
    errors: list[str] = []

    for sort_key in ("liquidity", "24-hour-vol"):
        resp, err, _ = _get_with_retry(
            client,
            search_url,
            params={"sort": sort_key, "filter": "open", "limit": limit_per_sort},
            max_retries=1,
        )
        if resp is not None:
            last_http = resp.status_code
        if err or resp is None:
            errors.append(f"sort={sort_key}: {err}")
            continue
        try:
            batch = resp.json()
        except ValueError as exc:
            errors.append(f"sort={sort_key}: Invalid JSON ({exc})")
            continue
        if isinstance(batch, list):
            for m in batch:
                mid = str(m.get("id") or "")
                if mid and not m.get("isResolved"):
                    raw_by_id[mid] = m

    markets: list[NormalizedMarket] = []
    multichoice_candidates: list[dict[str, Any]] = []

    for m in raw_by_id.values():
        otype = str(m.get("outcomeType") or "")
        liq = _safe_float(m.get("totalLiquidity")) or 0.0
        vol = _safe_float(m.get("volume")) or 0.0
        vol24 = _safe_float(m.get("volume24Hours")) or 0.0
        url = str(m.get("url") or "https://manifold.markets")
        q = str(m.get("question") or "").strip()
        close_iso = _epoch_ms_to_iso(m.get("closeTime"))

        if otype == "BINARY":
            prob = _clamp_prob(_safe_float(m.get("probability")))
            if prob is None:
                continue
            markets.append(
                NormalizedMarket(
                    venue="manifold",
                    currency="MANA_PLAY_MONEY",
                    market_id=str(m.get("id") or ""),
                    event_id=str(m.get("id") or ""),
                    event_title=q,
                    question=q,
                    outcome_label="",
                    category="manifold_binary",
                    url=url,
                    yes_bid=prob,
                    yes_ask=prob,
                    no_bid=round(1.0 - prob, 6),
                    no_ask=round(1.0 - prob, 6),
                    mid_prob=prob,
                    volume_24h=vol24,
                    volume_total=vol,
                    liquidity=liq,
                    fees_enabled=False,
                    taker_fee_rate=0.0,
                    fee_model="CPMM AMM (Mana play-money)",
                    end_date_iso=close_iso,
                )
            )
        elif otype == "MULTIPLE_CHOICE" and (liq >= 500.0 or vol24 >= 500.0):
            multichoice_candidates.append(m)

    # Expand top high-liquidity multi-choice Manifold markets so candidates/parties match Polymarket/Kalshi.
    multichoice_candidates.sort(
        key=lambda x: ((_safe_float(x.get("totalLiquidity")) or 0.0) + (_safe_float(x.get("volume24Hours")) or 0.0)),
        reverse=True,
    )
    for m in multichoice_candidates[:expand_top_multichoice]:
        mid = str(m.get("id") or "")
        resp, err, _ = _get_with_retry(client, f"{MANIFOLD_BASE_URL}/market/{mid}", max_retries=1)
        if err or resp is None:
            continue
        try:
            full = resp.json()
        except ValueError:
            continue
        answers = full.get("answers") or []
        q = str(full.get("question") or m.get("question") or "").strip()
        url = str(full.get("url") or m.get("url") or "https://manifold.markets")
        close_iso = _epoch_ms_to_iso(full.get("closeTime") or m.get("closeTime"))
        liq = _safe_float(full.get("totalLiquidity")) or _safe_float(m.get("totalLiquidity")) or 0.0
        vol24 = _safe_float(full.get("volume24Hours")) or _safe_float(m.get("volume24Hours")) or 0.0
        is_sum_one = bool(full.get("shouldAnswersSumToOne"))

        for ans in answers:
            if ans.get("resolution") is not None:
                continue
            prob = _clamp_prob(_safe_float(ans.get("probability")))
            text = str(ans.get("text") or "").strip()
            if prob is None or not text:
                continue
            ans_vol = _safe_float(ans.get("volume")) or 0.0
            markets.append(
                NormalizedMarket(
                    venue="manifold",
                    currency="MANA_PLAY_MONEY",
                    market_id=f"{mid}:{ans.get('id', '')}",
                    event_id=mid,
                    event_title=q,
                    question=f"{q} — {text}",
                    outcome_label=text,
                    category="manifold_multichoice",
                    url=url,
                    yes_bid=prob,
                    yes_ask=prob,
                    no_bid=round(1.0 - prob, 6),
                    no_ask=round(1.0 - prob, 6),
                    mid_prob=prob,
                    volume_24h=vol24,
                    volume_total=ans_vol,
                    liquidity=liq,
                    fees_enabled=False,
                    taker_fee_rate=0.0,
                    fee_model="CPMM AMM (Mana play-money)",
                    end_date_iso=close_iso,
                    is_mutually_exclusive_leg=is_sum_one,
                )
            )
        time.sleep(0.04)

    status = VenueStatus(
        venue="manifold",
        endpoint=search_url,
        ok=len(raw_by_id) > 0,
        http_status=last_http,
        latency_ms=round((time.monotonic() - t0) * 1000.0, 1),
        events_fetched=len(raw_by_id),
        markets_fetched=len(markets),
        currency="MANA_PLAY_MONEY",
        error="; ".join(errors) if errors else None,
        fetched_at_utc=fetched_at,
    )
    return markets, status


def create_http_client(timeout: float = 20.0) -> httpx.Client:
    """Create a polite httpx client with identifying User-Agent."""
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
