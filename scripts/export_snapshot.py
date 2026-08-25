#!/usr/bin/env python3
"""Export a PolyAlpha SQLite ledger into a static, browser-safe JSON snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def fetch_market(market_id: str) -> dict[str, Any]:
    context = ssl.create_default_context()
    if ssl.get_default_verify_paths().cafile is None and Path("/etc/ssl/cert.pem").exists():
        context.load_verify_locations(cafile="/etc/ssl/cert.pem")
    request = urllib.request.Request(
        f"https://gamma-api.polymarket.com/markets/{market_id}",
        headers={"Accept": "application/json", "User-Agent": "polyalpha-dashboard-export/0.1"},
    )
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        return json.loads(response.read())


def rows(db: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(query)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--skip-market-enrichment", action="store_true")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    specs = {agent["id"]: agent for agent in json.loads(Path(args.manifest).read_text())}

    agent_rows = rows(db, "SELECT * FROM agents ORDER BY id")
    trades = rows(db, "SELECT * FROM trades ORDER BY timestamp DESC, id DESC")
    positions = rows(db, "SELECT * FROM positions ORDER BY agent_id, market_id, outcome")
    orders = rows(db, "SELECT * FROM pending_orders ORDER BY created_at DESC, id DESC")
    equity = rows(db, "SELECT * FROM equity_snapshots ORDER BY timestamp, agent_id")

    trade_counts: dict[str, int] = defaultdict(int)
    position_counts: dict[str, int] = defaultdict(int)
    order_counts: dict[str, int] = defaultdict(int)
    exposure: dict[str, float] = defaultdict(float)
    for trade in trades:
        trade_counts[trade["agent_id"]] += 1
    for position in positions:
        position_counts[position["agent_id"]] += 1
        exposure[position["agent_id"]] += position["shares"] * position["avg_price"]
    for order in orders:
        order_counts[order["agent_id"]] += 1

    agents = []
    for agent in agent_rows:
        spec = specs.get(agent["id"], {})
        initial = float(agent["initial_cash"])
        high_water = max(0.01, float(agent["high_water"]))
        agents.append({
            **spec,
            "cash": agent["cash"],
            "equity": agent["equity"],
            "return_pct": 100 * (float(agent["equity"]) / initial - 1),
            "drawdown_pct": 100 * (1 - float(agent["equity"]) / high_water),
            "trades": trade_counts[agent["id"]],
            "positions": position_counts[agent["id"]],
            "pending_orders": order_counts[agent["id"]],
            "cost_exposure": exposure[agent["id"]],
        })

    market_ids = sorted({row["market_id"] for row in trades + positions + orders})
    market_data: dict[str, Any] = {}
    if not args.skip_market_enrichment:
        for index, market_id in enumerate(market_ids, 1):
            try:
                raw = fetch_market(market_id)
                event = (raw.get("events") or [{}])[0]
                market_data[market_id] = {
                    "id": market_id,
                    "question": raw.get("question") or f"Market {market_id}",
                    "slug": raw.get("slug") or "",
                    "category": raw.get("category") or event.get("category") or "Other",
                    "event": event.get("title") or "",
                    "active": bool(raw.get("active", False)),
                    "closed": bool(raw.get("closed", False)),
                    "end_date": raw.get("endDate"),
                    "liquidity": float(raw.get("liquidityNum") or raw.get("liquidity") or 0),
                    "volume_24h": float(raw.get("volume24hr") or 0),
                }
            except Exception:
                market_data[market_id] = {"id": market_id, "question": f"Market {market_id}", "slug": "", "category": "Other", "event": "", "active": False, "closed": False, "end_date": None, "liquidity": 0, "volume_24h": 0}
            if index % 20 == 0:
                time.sleep(0.05)
    else:
        market_data = {market_id: {"id": market_id, "question": f"Market {market_id}", "slug": "", "category": "Other", "event": "", "active": False, "closed": False, "end_date": None, "liquidity": 0, "volume_24h": 0} for market_id in market_ids}

    output = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "Polymarket public-data paper trading",
            "starting_cash_per_agent": 10_000,
            "currency": "USDC",
            "disclaimer": "Paper-trading research only. Simulated performance is not evidence of future returns.",
        },
        "summary": {
            "agents": len(agents),
            "trades": len(trades),
            "positions": len(positions),
            "pending_orders": len(orders),
            "markets_traded": len(market_ids),
            "aggregate_equity": sum(float(agent["equity"]) for agent in agents),
            "aggregate_starting_cash": sum(float(agent["initial_cash"]) for agent in agent_rows),
            "agents_with_trades": sum(1 for agent in agents if agent["trades"] > 0),
        },
        "agents": agents,
        "trades": trades,
        "positions": positions,
        "orders": orders,
        "equity": equity,
        "markets": market_data,
    }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, separators=(",", ":")) + "\n")
    print(f"Wrote {destination} ({destination.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
