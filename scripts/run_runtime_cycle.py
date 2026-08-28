#!/usr/bin/env python3
"""Run one atomic public-data paper cycle and publish browser-safe datasets."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from polyalpha.agents import build_agents
from polyalpha.api import PolymarketClient
from polyalpha.broker import PaperBroker
from polyalpha.engine import TradingEngine
from polyalpha.factory import build_agent_specs
from polyalpha.news import NewsClient


def write_failure_health(path: Path, error: Exception) -> None:
    prior: dict = {}
    if path.exists():
        try:
            prior = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            prior = {}
    now = time.time()
    prior.update({
        "status": "degraded",
        "last_attempt_at": now,
        "age_seconds": (
            max(0.0, now - float(prior["last_success_at"]))
            if prior.get("last_success_at") else None
        ),
        "error": str(error)[:1000],
    })
    prior.setdefault("meta", {})["generated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prior, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--manifest", default="engine/agents.json")
    parser.add_argument("--skip-market-enrichment", action="store_true")
    args = parser.parse_args()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    health_path = output / "health.json"
    broker = PaperBroker(args.db)
    try:
        engine = TradingEngine(
            PolymarketClient(), build_agents(build_agent_specs()), broker,
            news_client=NewsClient(),
        )
        report = engine.cycle()
        broker.checkpoint()
    except Exception as exc:
        try:
            broker.checkpoint()
        finally:
            broker.close()
        write_failure_health(health_path, exc)
        print(f"Runtime cycle failed: {exc}", file=sys.stderr)
        return 2
    else:
        broker.close()

    command = [
        sys.executable,
        "scripts/export_snapshot.py",
        "--db", args.db,
        "--manifest", args.manifest,
        "--output", str(output / "dashboard.json"),
        "--trades-output", str(output / "trades.json"),
        "--equity-output", str(output / "equity.json"),
        "--health-output", str(health_path),
        "--epoch", "v2-edge-only",
        "--label", "V2.4 · Executable adaptive alpha",
    ]
    if args.skip_market_enrichment:
        command.append("--skip-market-enrichment")
    subprocess.run(command, check=True)
    print(
        json.dumps({
            "ok": True,
            "cycle_id": report.cycle_id,
            "markets": report.markets_discovered,
            "books": report.markets_with_books,
            "alpha_fills": report.alpha_fills,
            "heartbeat_fills": report.heartbeat_fills,
            "retirement_fills": report.retirement_fills,
            "news_items": report.news_items,
            "history_points": report.history_points,
            "history_ready_markets": report.history_ready_markets,
            "signals_generated": report.signals_generated,
            "executable_signals": report.executable_signals,
            "risk_rejections": report.risk_rejections,
            "risk_rejection_reasons": report.risk_rejection_reasons,
            "counterfactuals_recorded": report.counterfactuals_recorded,
            "adaptation_resolved": report.adaptation_resolved,
            "strategies_paused": report.strategies_paused,
            "news_confirmed_markets": report.news_confirmed_markets,
            "news_signal_overlays": report.news_signal_overlays,
            "news_errors": report.news_errors,
            "elapsed_seconds": report.elapsed_seconds,
        })
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
