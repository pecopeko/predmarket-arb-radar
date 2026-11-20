"""Strict Pydantic v2 data models for PredMarket Arb Radar."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

VenueName = Literal["polymarket", "kalshi", "manifold"]
CurrencyKind = Literal["USD", "MANA_PLAY_MONEY"]
ExhaustivenessStatus = Literal[
    "EXHAUSTIVE_DUTCH_BOOK",
    "EXHAUSTIVE_EFFICIENT",
    "NON_EXHAUSTIVE_AUGMENTED_TRAP",
    "NON_EXHAUSTIVE_OPEN_FIELD",
]
SignalClass = Literal[
    "PURE_CROSS_VENUE_ARB",
    "NEAR_ARB_MAKER_CANDIDATE",
    "HIGH_LIQ_DIVERGENCE",
    "CROSS_VENUE_TRACKED",
]


class VenueStatus(BaseModel):
    """Exact live provenance and HTTP health status for each venue endpoint."""

    venue: str
    endpoint: str
    ok: bool
    http_status: Optional[int] = None
    latency_ms: float = 0.0
    events_fetched: int = 0
    markets_fetched: int = 0
    currency: CurrencyKind = "USD"
    error: Optional[str] = None
    fetched_at_utc: str = ""


class OrderBookLevel(BaseModel):
    """Single price/size level on an order book."""

    price: float = Field(..., ge=0.0, le=1.0)
    size: float = Field(..., ge=0.0)


class OrderBookSnapshot(BaseModel):
    """Normalized order book snapshot with asks sorted ascending and bids descending."""

    venue: VenueName
    market_id: str
    token_id: str = ""
    best_bid: Optional[float] = None
    best_bid_size: Optional[float] = None
    best_ask: Optional[float] = None
    best_ask_size: Optional[float] = None
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    source: str = ""
    http_status: Optional[int] = None
    error: Optional[str] = None


class NormalizedMarket(BaseModel):
    """Unified binary proposition across Polymarket, Kalshi, and Manifold."""

    venue: VenueName
    currency: CurrencyKind
    market_id: str
    event_id: str
    event_title: str
    question: str
    outcome_label: str = ""
    category: str = ""
    url: str = ""

    # Executable top-of-book prices (None when genuinely unavailable — never fabricated!)
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    mid_prob: Optional[float] = None

    # Order book top-of-book fillable sizes (shares / contracts, None when unverified)
    yes_ask_size: Optional[float] = None
    no_ask_size: Optional[float] = None
    yes_bid_size: Optional[float] = None

    # Activity / liquidity provenance
    volume_24h: float = 0.0
    volume_total: float = 0.0
    liquidity: float = 0.0
    open_interest: float = 0.0

    # Fee metadata sourced directly from exchange API
    fees_enabled: bool = True
    taker_fee_rate: float = 0.0
    fee_model: str = ""

    # Structural & settlement metadata
    end_date_iso: Optional[str] = None
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    is_mutually_exclusive_leg: bool = False
    is_neg_risk_other: bool = False
    rules_summary: str = ""


class MultiOutcomeEvent(BaseModel):
    """Multi-outcome event basket on Polymarket or Kalshi for Dutch-book analysis."""

    venue: VenueName
    currency: CurrencyKind = "USD"
    event_id: str
    slug: str = ""
    title: str
    category: str = ""
    url: str = ""
    end_date_iso: Optional[str] = None

    # Mutually-exclusive / exhaustiveness metadata from venue API
    is_mutually_exclusive: bool = False
    neg_risk: bool = False
    neg_risk_augmented: bool = False
    total_markets_in_event: int = 0
    active_markets_count: int = 0
    closed_markets_count: int = 0
    inactive_placeholder_count: int = 0
    has_excluded_other_leg: bool = False
    has_tradable_other_leg: bool = False

    volume_24h: float = 0.0
    liquidity: float = 0.0
    legs: list[NormalizedMarket] = Field(default_factory=list)


class DutchBookLeg(BaseModel):
    """Single outcome leg inside a Dutch-book scan result."""

    market_id: str
    label: str
    yes_ask: float
    taker_fee: float
    yes_ask_size: Optional[float] = None
    token_id: Optional[str] = None
    book_verified: bool = False


class DutchBookOpportunity(BaseModel):
    """Evaluated intra-venue multi-outcome Dutch-book basket."""

    event_id: str
    venue: VenueName
    title: str
    url: str = ""
    category: str = ""
    end_date_iso: Optional[str] = None

    exhaustiveness_status: ExhaustivenessStatus
    is_truly_exhaustive: bool
    exhaustiveness_reason: str

    leg_count: int
    total_markets_in_event: int
    inactive_placeholder_count: int = 0
    sum_yes_asks: float
    total_taker_fees: float
    slippage_buffer: float = 0.0
    total_all_in_cost: float

    gross_edge_pct: float
    net_roi_pct: float
    is_net_arb: bool

    # Depth-capped sizing (None when orderbook depth unavailable)
    book_verified_legs: int = 0
    max_fillable_shares: Optional[float] = None
    max_fillable_usd: Optional[float] = None
    expected_net_profit_usd: Optional[float] = None

    volume_24h: float = 0.0
    liquidity: float = 0.0
    legs: list[DutchBookLeg] = Field(default_factory=list)


class CrossVenueMatch(BaseModel):
    """Matched proposition across two venues with exact arb & divergence metrics."""

    match_id: str
    event_family: str
    canonical_label: str
    target_year: Optional[int] = None
    target_month: Optional[str] = None
    threshold_value: Optional[float] = None
    threshold_direction: Optional[str] = None

    venue_a: VenueName
    venue_b: VenueName
    currency_a: CurrencyKind
    currency_b: CurrencyKind
    is_cash_arb_eligible: bool

    market_a_id: str
    market_b_id: str
    title_a: str
    title_b: str
    url_a: str = ""
    url_b: str = ""
    end_date_iso: Optional[str] = None

    # Prices & implied probabilities
    prob_a: float
    prob_b: float
    prob_diff_pp: float

    yes_ask_a: Optional[float] = None
    no_ask_a: Optional[float] = None
    yes_ask_b: Optional[float] = None
    no_ask_b: Optional[float] = None

    # Best cross-venue hedge (only computed when cash_arb_eligible and asks present)
    best_direction: str = ""
    leg_a_action: str = ""
    leg_b_action: str = ""
    leg_a_price: Optional[float] = None
    leg_b_price: Optional[float] = None
    fee_a: float = 0.0
    fee_b: float = 0.0
    slippage_buffer: float = 0.0
    gross_cost: Optional[float] = None
    net_cost: Optional[float] = None
    gross_edge_pct: Optional[float] = None
    net_roi_pct: Optional[float] = None
    annualized_net_roi_pct: Optional[float] = None
    days_to_expiry: Optional[float] = None

    # Orderbook-verified sizing
    leg_a_ask_size: Optional[float] = None
    leg_b_ask_size: Optional[float] = None
    max_fillable_shares: Optional[float] = None
    max_fillable_usd: Optional[float] = None
    expected_net_profit_usd: Optional[float] = None
    book_verified: bool = False

    # Liquidity & volume
    volume_24h_a: float = 0.0
    volume_24h_b: float = 0.0
    liquidity_a: float = 0.0
    liquidity_b: float = 0.0

    signal_class: SignalClass
    match_confidence: float = 0.0
    risk_flags: list[str] = Field(default_factory=list)


class AlertRecord(BaseModel):
    """Deduplicated outbound alert record."""

    alert_id: str
    signal_type: str
    title: str
    edge_bps: int
    sent_at_utc: str
    channel: str = "dry_run"


class ScanReport(BaseModel):
    """Complete live scan payload serialized to docs/latest_scan.json."""

    schema_version: str = "1.0.0"
    generated_at_utc: str
    scan_duration_sec: float
    slippage_bps: float = 0.0
    venue_statuses: list[VenueStatus] = Field(default_factory=list)

    summary: dict[str, int | float] = Field(default_factory=dict)
    dutch_books: list[DutchBookOpportunity] = Field(default_factory=list)
    cross_venue_matches: list[CrossVenueMatch] = Field(default_factory=list)
    sample_discord_embeds: list[dict] = Field(default_factory=list)
