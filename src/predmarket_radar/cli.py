"""CLI entrypoint for PredMarket Arb Radar."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .alerter import AlertDeduplicator, dispatch_alerts
from .dashboard_builder import build_live_dashboard, run_live_scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="predmarket-radar",
        description="Read-only Polymarket / Kalshi / Manifold arbitrage & divergence radar.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Run a live scan and print JSON summary.")
    p_scan.add_argument("--slippage-bps", type=float, default=float(os.getenv("RADAR_SLIPPAGE_BPS", "0.0")))
    p_scan.add_argument("--min-div-pp", type=float, default=float(os.getenv("RADAR_MIN_DIVERGENCE_PP", "3.0")))
    p_scan.add_argument("--no-books", action="store_true", help="Skip live CLOB/Kalshi orderbook enrichment.")
    p_scan.add_argument("--out", type=Path, default=None, help="Optional JSON output path.")

    p_dash = sub.add_parser("build-dashboard", help="Run a live scan and build docs/ static dashboard.")
    p_dash.add_argument("--docs-dir", type=Path, default=Path("docs"))
    p_dash.add_argument("--slippage-bps", type=float, default=float(os.getenv("RADAR_SLIPPAGE_BPS", "0.0")))
    p_dash.add_argument("--min-div-pp", type=float, default=float(os.getenv("RADAR_MIN_DIVERGENCE_PP", "3.0")))
    p_dash.add_argument("--no-books", action="store_true", help="Skip live CLOB/Kalshi orderbook enrichment.")
    p_dash.add_argument("--alert", action="store_true", help="Also run alert dispatcher (dry-run if webhooks absent).")
    p_dash.add_argument("--state-file", type=Path, default=Path("state/alert_state.json"))
    p_dash.add_argument("--cooldown-sec", type=int, default=int(os.getenv("RADAR_ALERT_COOLDOWN_SEC", "21600")))

    args = parser.parse_args(argv)

    if args.cmd == "scan":
        report = run_live_scan(
            slippage_bps=args.slippage_bps,
            min_divergence_pp=args.min_div_pp,
            enrich_orderbooks=not args.no_books,
        )
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(report.model_dump(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        print(json.dumps({"generated_at_utc": report.generated_at_utc, "summary": report.summary}, indent=2))
        return 0

    if args.cmd == "build-dashboard":
        report, paths = build_live_dashboard(
            docs_dir=args.docs_dir,
            slippage_bps=args.slippage_bps,
            min_divergence_pp=args.min_div_pp,
            enrich_orderbooks=not args.no_books,
        )
        output: dict = {
            "generated_at_utc": report.generated_at_utc,
            "summary": report.summary,
            "artifacts": paths,
        }
        if args.alert:
            discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None
            tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
            tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
            dry_run = not bool(discord_url or (tg_token and tg_chat))
            dedup = AlertDeduplicator(
                state_path=args.state_file,
                cooldown_seconds=args.cooldown_sec,
            )
            alert_res = dispatch_alerts(
                report=report,
                deduplicator=dedup,
                discord_webhook_url=discord_url,
                telegram_bot_token=tg_token,
                telegram_chat_id=tg_chat,
                dry_run=dry_run,
            )
            output["alerts"] = {
                "dry_run": alert_res["dry_run"],
                "alerts_selected": alert_res["alerts_selected"],
                "delivered_discord": alert_res["delivered_discord"],
                "delivered_telegram": alert_res["delivered_telegram"],
                "delivery_errors": alert_res["delivery_errors"],
            }
        print(json.dumps(output, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
