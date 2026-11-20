"""Prediction Market Cross-Platform Arbitrage & Mispricing Radar.

Strict read-only public market data scanner across Polymarket (Gamma + CLOB),
Kalshi Public Trade API v2, and Manifold Markets. Zero custody, zero trade
execution, zero paid API keys.
"""

__version__ = "1.0.0"

from .models import (
    AlertRecord,
    CrossVenueMatch,
    DutchBookLeg,
    DutchBookOpportunity,
    MultiOutcomeEvent,
    NormalizedMarket,
    OrderBookLevel,
    OrderBookSnapshot,
    ScanReport,
    VenueStatus,
)

__all__ = [
    "__version__",
    "AlertRecord",
    "CrossVenueMatch",
    "DutchBookLeg",
    "DutchBookOpportunity",
    "MultiOutcomeEvent",
    "NormalizedMarket",
    "OrderBookLevel",
    "OrderBookSnapshot",
    "ScanReport",
    "VenueStatus",
]
