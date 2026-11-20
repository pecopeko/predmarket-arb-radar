"""Discord & Telegram alert formatting, deduplication state store, and dry-run-safe dispatcher."""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .models import AlertRecord, CrossVenueMatch, DutchBookOpportunity, ScanReport
from .venues import utc_now_iso

COLOR_EMERALD = 0x0D7A5F
COLOR_COBALT = 0x1D4ED8
COLOR_AMBER = 0xD97706
COLOR_CRIMSON = 0xB91C1C

RISK_DISCLAIMER = (
    "Informational market data only — NOT financial advice. Cross-venue trades carry legged-in "
    "execution risk, capital lockup, and oracle/resolution rule basis risk. Verify orderbooks before acting."
)


def format_cross_venue_discord_embed(match: CrossVenueMatch) -> dict[str, Any]:
    """Build a rich Discord webhook embed for a cross-venue arbitrage or divergence signal."""
    if match.signal_class == "PURE_CROSS_VENUE_ARB":
        color = COLOR_EMERALD
        badge = "PURE CROSS-VENUE ARB"
    elif match.signal_class == "NEAR_ARB_MAKER_CANDIDATE":
        color = COLOR_COBALT
        badge = "MAKER-ONLY NEAR ARB"
    else:
        color = COLOR_AMBER
        badge = "PROBABILITY DIVERGENCE"

    venue_a_label = match.venue_a.title()
    venue_b_label = match.venue_b.title()
    title = f"[{badge}] {match.canonical_label}"[:250]

    lines = [
        f"**{venue_a_label}** ({match.currency_a}): `{match.prob_a * 100:.1f}%`",
        f"**{venue_b_label}** ({match.currency_b}): `{match.prob_b * 100:.1f}%`",
        f"**Implied Spread:** `{match.prob_diff_pp:.2f} pp`",
    ]
    if match.best_direction:
        lines.append(f"**Best Hedge:** `{match.best_direction}`")

    fields: list[dict[str, Any]] = []
    if match.is_cash_arb_eligible and match.gross_cost is not None and match.net_cost is not None:
        leg_a_p = f"${match.leg_a_price:.4f}" if match.leg_a_price is not None else "—"
        leg_b_p = f"${match.leg_b_price:.4f}" if match.leg_b_price is not None else "—"
        fields.append(
            {
                "name": "Executable Legs",
                "value": (
                    f"{match.leg_a_action} {venue_a_label} @ `{leg_a_p}` (fee `${match.fee_a:.4f}`)\n"
                    f"{match.leg_b_action} {venue_b_label} @ `{leg_b_p}` (fee `${match.fee_b:.4f}`)"
                ),
                "inline": False,
            }
        )
        net_roi_str = f"{match.net_roi_pct:+.2f}%" if match.net_roi_pct is not None else "—"
        ann_str = f"{match.annualized_net_roi_pct:+.1f}%/yr" if match.annualized_net_roi_pct is not None else "—"
        fields.append(
            {
                "name": "All-In Cost vs $1.00",
                "value": f"Gross `${match.gross_cost:.4f}` -> Net `${match.net_cost:.4f}`",
                "inline": True,
            }
        )
        fields.append(
            {
                "name": "Fee-Adjusted Net ROI",
                "value": f"`{net_roi_str}` (Ann: `{ann_str}`)",
                "inline": True,
            }
        )
        if match.max_fillable_usd is not None and match.expected_net_profit_usd is not None:
            fields.append(
                {
                    "name": "Book-Capped Size & Est. PnL",
                    "value": (
                        f"`{match.max_fillable_shares:,.1f}` sh (`${match.max_fillable_usd:,.2f}`) | "
                        f"Est. Net: `${match.expected_net_profit_usd:+,.2f}`"
                    ),
                    "inline": False,
                }
            )
    else:
        fields.append(
            {
                "name": "24h Activity / Liquidity",
                "value": (
                    f"{venue_a_label}: Vol24 `${match.volume_24h_a:,.0f}` / Liq `${match.liquidity_a:,.0f}`\n"
                    f"{venue_b_label}: Vol24 `{match.volume_24h_b:,.0f}` / Liq `{match.liquidity_b:,.0f}`"
                ),
                "inline": False,
            }
        )

    if match.risk_flags:
        fields.append(
            {
                "name": "Execution & Basis Risk Flags",
                "value": ", ".join(match.risk_flags[:4])[:1000],
                "inline": False,
            }
        )

    links = []
    if match.url_a:
        links.append(f"[{venue_a_label} Market]({match.url_a})")
    if match.url_b:
        links.append(f"[{venue_b_label} Market]({match.url_b})")
    if links:
        fields.append({"name": "Venue Links", "value": " | ".join(links), "inline": False})

    return {
        "title": title,
        "description": "\n".join(lines),
        "color": color,
        "fields": fields,
        "footer": {"text": f"PredMarket Arb Radar | {RISK_DISCLAIMER}"[:2048]},
        "timestamp": utc_now_iso(),
    }


def format_dutch_book_discord_embed(opp: DutchBookOpportunity) -> dict[str, Any]:
    """Build a rich Discord webhook embed for a multi-outcome Dutch-book or trap warning."""
    if opp.is_net_arb:
        color = COLOR_EMERALD
        badge = "EXHAUSTIVE DUTCH-BOOK ARB"
    elif not opp.is_truly_exhaustive:
        color = COLOR_CRIMSON
        badge = "NON-EXHAUSTIVE TRAP WARNING"
    else:
        color = COLOR_AMBER
        badge = "DUTCH-BOOK WATCH"

    title = f"[{badge}] {opp.venue.title()}: {opp.title}"[:250]
    size_str = (
        f"`{opp.max_fillable_shares:,.1f}` baskets (`${opp.max_fillable_usd:,.2f}`)"
        if opp.max_fillable_usd is not None and opp.max_fillable_shares is not None
        else "Unverified (inspect CLOB)"
    )
    pnl_str = f"`${opp.expected_net_profit_usd:+,.2f}`" if opp.expected_net_profit_usd is not None else "—"

    top_legs = sorted(opp.legs, key=lambda x: x.yes_ask, reverse=True)[:5]
    leg_lines = [f"- {leg.label}: `${leg.yes_ask:.4f}`" for leg in top_legs]
    if len(opp.legs) > 5:
        leg_lines.append(f"- ... +{len(opp.legs) - 5} more active legs")

    fields = [
        {
            "name": "Basket Pricing",
            "value": (
                f"Sum YES Asks: `${opp.sum_yes_asks:.4f}` | Fees: `${opp.total_taker_fees:.4f}` | "
                f"All-In: `${opp.total_all_in_cost:.4f}`"
            ),
            "inline": False,
        },
        {"name": "Gross Edge / Net ROI", "value": f"`{opp.gross_edge_pct:+.2f}%` / `{opp.net_roi_pct:+.2f}%`", "inline": True},
        {"name": "Book-Capped Size & Est. PnL", "value": f"{size_str} | {pnl_str}", "inline": True},
        {"name": "Exhaustiveness Audit", "value": opp.exhaustiveness_reason[:1000], "inline": False},
        {"name": f"Top Legs ({opp.leg_count}/{opp.total_markets_in_event})", "value": "\n".join(leg_lines), "inline": False},
    ]
    if opp.url:
        fields.append({"name": "Venue Link", "value": f"[Open on {opp.venue.title()}]({opp.url})", "inline": False})

    return {
        "title": title,
        "description": f"Multi-outcome basket scan on **{opp.venue.title()}** (`{opp.exhaustiveness_status}`).",
        "color": color,
        "fields": fields,
        "footer": {"text": f"PredMarket Arb Radar | {RISK_DISCLAIMER}"[:2048]},
        "timestamp": utc_now_iso(),
    }


def format_cross_venue_telegram_html(match: CrossVenueMatch) -> str:
    """Format a safe HTML Telegram Bot API message for a cross-venue signal."""
    label = html.escape(match.canonical_label)
    va = html.escape(match.venue_a.title())
    vb = html.escape(match.venue_b.title())
    sig = html.escape(match.signal_class)
    parts = [
        f"<b>[{sig}]</b> {label}",
        f"{va}: <code>{match.prob_a * 100:.1f}%</code> | {vb}: <code>{match.prob_b * 100:.1f}%</code> (Δ <code>{match.prob_diff_pp:.2f}pp</code>)",
    ]
    if match.is_cash_arb_eligible and match.net_cost is not None and match.net_roi_pct is not None:
        hedge = html.escape(match.best_direction)
        parts.append(f"Hedge: <code>{hedge}</code>")
        parts.append(f"All-In Cost: <code>${match.net_cost:.4f}</code> | Net ROI: <code>{match.net_roi_pct:+.2f}%</code>")
    if match.risk_flags:
        parts.append("Risks: " + html.escape(", ".join(match.risk_flags[:3])))
    return "\n".join(parts)


class AlertDeduplicator:
    """Persistent JSON state store preventing duplicate spread alert spam."""

    def __init__(
        self,
        state_path: Optional[Path] = None,
        cooldown_seconds: int = 21600,
        min_edge_improvement_bps: int = 100,
    ) -> None:
        self.state_path = state_path
        self.cooldown_seconds = cooldown_seconds
        self.min_edge_improvement_bps = min_edge_improvement_bps
        self._state: dict[str, dict[str, Any]] = {}
        if state_path is not None and state_path.exists():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._state = loaded
            except (OSError, ValueError):
                self._state = {}

    def should_alert(
        self,
        alert_id: str,
        edge_bps: int,
        now_ts: Optional[float] = None,
    ) -> bool:
        """Return True iff alert_id is unseen, past cooldown, or improved materially."""
        now = time.time() if now_ts is None else now_ts
        prev = self._state.get(alert_id)
        if prev is None:
            return True
        elapsed = now - float(prev.get("sent_ts", 0.0))
        if elapsed >= self.cooldown_seconds:
            return True
        prev_edge = int(prev.get("edge_bps", 0))
        if (edge_bps - prev_edge) >= self.min_edge_improvement_bps:
            return True
        return False

    def record_sent(
        self,
        alert_id: str,
        signal_type: str,
        title: str,
        edge_bps: int,
        channel: str = "dry_run",
        now_ts: Optional[float] = None,
    ) -> AlertRecord:
        now = time.time() if now_ts is None else now_ts
        sent_iso = utc_now_iso()
        self._state[alert_id] = {
            "signal_type": signal_type,
            "title": title,
            "edge_bps": edge_bps,
            "sent_ts": now,
            "sent_at_utc": sent_iso,
            "channel": channel,
        }
        self._save()
        return AlertRecord(
            alert_id=alert_id,
            signal_type=signal_type,
            title=title,
            edge_bps=edge_bps,
            sent_at_utc=sent_iso,
            channel=channel,
        )

    def _save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")


def build_sample_discord_embeds(
    report_matches: list[CrossVenueMatch],
    report_dutch: list[DutchBookOpportunity],
    max_embeds: int = 4,
) -> list[dict[str, Any]]:
    """Generate representative Discord embed previews for the dashboard and docs."""
    embeds: list[dict[str, Any]] = []
    for m in report_matches:
        if len(embeds) >= max_embeds - 1:
            break
        if m.signal_class in ("PURE_CROSS_VENUE_ARB", "NEAR_ARB_MAKER_CANDIDATE", "HIGH_LIQ_DIVERGENCE"):
            embeds.append(format_cross_venue_discord_embed(m))
    for d in report_dutch:
        if len(embeds) >= max_embeds:
            break
        if d.is_net_arb or d.exhaustiveness_status == "NON_EXHAUSTIVE_AUGMENTED_TRAP":
            embeds.append(format_dutch_book_discord_embed(d))
    return embeds


def dispatch_alerts(
    report: ScanReport,
    deduplicator: AlertDeduplicator,
    discord_webhook_url: Optional[str] = None,
    telegram_bot_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    min_divergence_alert_pp: float = 5.0,
    dry_run: bool = True,
    client: Optional[httpx.Client] = None,
) -> dict[str, Any]:
    """Select deduplicated alerts and deliver to Discord/Telegram (or dry-run safely)."""
    embeds: list[dict[str, Any]] = []
    tg_messages: list[str] = []
    selected_meta: list[tuple[str, str, str, int]] = []

    for m in report.cross_venue_matches:
        is_actionable = m.signal_class == "PURE_CROSS_VENUE_ARB" or (
            m.signal_class == "HIGH_LIQ_DIVERGENCE" and m.prob_diff_pp >= min_divergence_alert_pp
        )
        if not is_actionable:
            continue
        edge_bps = int(round((m.net_roi_pct if m.net_roi_pct is not None and m.net_roi_pct > 0 else m.prob_diff_pp) * 100))
        aid = f"xvenue:{m.match_id}:{m.signal_class}"
        if not deduplicator.should_alert(aid, edge_bps):
            continue
        embeds.append(format_cross_venue_discord_embed(m))
        tg_messages.append(format_cross_venue_telegram_html(m))
        selected_meta.append((aid, m.signal_class, m.canonical_label, edge_bps))

    for d in report.dutch_books:
        if not d.is_net_arb:
            continue
        edge_bps = int(round(d.net_roi_pct * 100))
        aid = f"dutch:{d.venue}:{d.event_id}"
        if not deduplicator.should_alert(aid, edge_bps):
            continue
        embeds.append(format_dutch_book_discord_embed(d))
        selected_meta.append((aid, "EXHAUSTIVE_DUTCH_BOOK", d.title, edge_bps))

    records: list[AlertRecord] = []
    delivery_errors: list[str] = []
    delivered_discord = 0
    delivered_telegram = 0

    discord_payload = {
        "username": "PredMarket Arb Radar",
        "content": f"**PredMarket Arb Radar Live Scan** (`{report.generated_at_utc}`) — {len(embeds)} new signal(s)",
        "embeds": embeds[:10],
    }

    if not dry_run and embeds:
        own_client = client is None
        http = client or httpx.Client(timeout=15.0)
        try:
            if discord_webhook_url:
                for i in range(0, len(embeds), 10):
                    batch = {
                        "username": "PredMarket Arb Radar",
                        "embeds": embeds[i : i + 10],
                    }
                    try:
                        r = http.post(discord_webhook_url, json=batch)
                        if r.status_code in (200, 204):
                            delivered_discord += len(batch["embeds"])
                        else:
                            delivery_errors.append(f"Discord HTTP {r.status_code}: {r.text[:160]}")
                    except httpx.HTTPError as exc:
                        delivery_errors.append(f"Discord error: {exc}")

            if telegram_bot_token and telegram_chat_id:
                tg_url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                for msg in tg_messages[:10]:
                    try:
                        r = http.post(
                            tg_url,
                            json={
                                "chat_id": telegram_chat_id,
                                "text": msg,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True,
                            },
                        )
                        if r.status_code == 200:
                            delivered_telegram += 1
                        else:
                            delivery_errors.append(f"Telegram HTTP {r.status_code}: {r.text[:160]}")
                    except httpx.HTTPError as exc:
                        delivery_errors.append(f"Telegram error: {exc}")
        finally:
            if own_client:
                http.close()

    channel_label = "dry_run" if dry_run else ("webhook" if (delivered_discord or delivered_telegram) else "no_webhook")
    if dry_run or not delivery_errors:
        for aid, sig_type, title, edge_bps in selected_meta:
            records.append(
                deduplicator.record_sent(
                    alert_id=aid,
                    signal_type=sig_type,
                    title=title,
                    edge_bps=edge_bps,
                    channel=channel_label,
                )
            )

    return {
        "dry_run": dry_run,
        "alerts_selected": len(selected_meta),
        "delivered_discord": delivered_discord,
        "delivered_telegram": delivered_telegram,
        "delivery_errors": delivery_errors,
        "discord_payload": discord_payload if embeds else None,
        "telegram_messages": tg_messages,
        "records": [r.model_dump() for r in records],
    }
