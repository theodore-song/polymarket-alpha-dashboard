#!/usr/bin/env python3
"""Export a PolyAlpha SQLite ledger into a static, browser-safe JSON snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import statistics
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
    parser.add_argument("--epoch", default="v2-edge-only")
    parser.add_argument("--label", default="V2 · Edge-only restart")
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
    fees: dict[str, float] = defaultdict(float)
    turnover: dict[str, float] = defaultdict(float)
    for trade in trades:
        trade_counts[trade["agent_id"]] += 1
        fees[trade["agent_id"]] += float(trade.get("fee") or 0)
        if trade.get("side") in {"BUY", "SELL"}:
            turnover[trade["agent_id"]] += float(trade["shares"]) * float(trade["price"])
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
            "allocation_status": spec.get("allocation_status", agent.get("allocation_status", "active")),
            "allocation_tier": spec.get("allocation_tier", agent.get("allocation_tier", "probation")),
            "strategy_version": spec.get("strategy_version", agent.get("strategy_version", args.epoch)),
            "fees_paid": fees[agent["id"]],
            "turnover": turnover[agent["id"]],
            "liquidation_value": float(agent["equity"]) - float(agent["cash"]),
            "unrealized_pnl": float(agent["equity"]) - float(agent["cash"]) - exposure[agent["id"]],
        })
        agents[-1]["realized_pnl"] = (
            float(agent["equity"]) - initial - agents[-1]["unrealized_pnl"]
        )

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
                    "event_id": str(event.get("id") or raw.get("eventId") or market_id),
                    "active": bool(raw.get("active", False)),
                    "closed": bool(raw.get("closed", False)),
                    "end_date": raw.get("endDate"),
                    "liquidity": float(raw.get("liquidityNum") or raw.get("liquidity") or 0),
                    "volume_24h": float(raw.get("volume24hr") or 0),
                }
            except Exception:
                market_data[market_id] = {"id": market_id, "question": f"Market {market_id}", "slug": "", "category": "Other", "event": "", "event_id": market_id, "active": False, "closed": False, "end_date": None, "liquidity": 0, "volume_24h": 0}
            if index % 20 == 0:
                time.sleep(0.05)
    else:
        market_data = {market_id: {"id": market_id, "question": f"Market {market_id}", "slug": "", "category": "Other", "event": "", "event_id": market_id, "active": False, "closed": False, "end_date": None, "liquidity": 0, "volume_24h": 0} for market_id in market_ids}

    by_agent_trades: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_agent_trades[trade["agent_id"]].append(trade)
    now = time.time()
    for agent in agents:
        agent_trades = sorted(by_agent_trades[agent["id"]], key=lambda row: (row["timestamp"], row["id"]))
        first_trade = min((float(row["timestamp"]) for row in agent_trades), default=now)
        settlements = [row for row in agent_trades if row["side"] == "SETTLE"]
        categories = {market_data.get(row["market_id"], {}).get("category", "Other") for row in settlements}
        resolved_edges: list[float] = []
        model_brier: list[float] = []
        market_brier: list[float] = []
        event_pnl: dict[str, float] = defaultdict(float)
        settled_markets = {row["market_id"] for row in settlements}
        for market_id in settled_markets:
            market_rows = [row for row in agent_trades if row["market_id"] == market_id]
            settlement = next((row for row in reversed(market_rows) if row["side"] == "SETTLE"), None)
            entry = next((row for row in market_rows if row["side"] == "BUY"), None)
            if not settlement or not entry:
                continue
            actual_yes = float(settlement["price"]) if str(entry["outcome"]).lower() == "yes" else 1.0 - float(settlement["price"])
            if entry.get("estimated_yes_probability") is not None:
                model_brier.append((float(entry["estimated_yes_probability"]) - actual_yes) ** 2)
            if entry.get("market_yes_probability") is not None:
                market_brier.append((float(entry["market_yes_probability"]) - actual_yes) ** 2)
            if entry.get("net_edge") is not None:
                resolved_edges.append(float(entry["net_edge"]))
            cash_flow = 0.0
            for row in market_rows:
                value = float(row["shares"]) * float(row["price"])
                cash_flow += value - float(row.get("fee") or 0) if row["side"] in {"SELL", "SETTLE"} else -value - float(row.get("fee") or 0)
            event_id = str(market_data.get(market_id, {}).get("event_id") or market_id)
            event_pnl[event_id] += cash_flow

        edge_lcb = None
        if resolved_edges:
            spread_error = statistics.stdev(resolved_edges) / len(resolved_edges) ** 0.5 if len(resolved_edges) > 1 else 0.0
            edge_lcb = statistics.mean(resolved_edges) - 1.96 * spread_error
        brier_improvement = None
        if model_brier and len(model_brier) == len(market_brier):
            brier_improvement = statistics.mean(market_brier) - statistics.mean(model_brier)
        positive_event_pnl = [value for value in event_pnl.values() if value > 0]
        event_concentration = (
            max(positive_event_pnl) / sum(positive_event_pnl) if positive_event_pnl else None
        )
        checks = {
            "resolved_sample": len(settled_markets) >= 100,
            "observation_window": (now - first_trade) / 86400 >= 28,
            "category_breadth": len(categories) >= 3,
            "positive_net_return": float(agent["return_pct"]) > 0,
            "positive_edge_lcb": edge_lcb is not None and edge_lcb > 0,
            "calibration_improvement": brier_improvement is not None and brier_improvement > 0,
            "drawdown_control": float(agent["drawdown_pct"]) < 12,
            "event_diversification": event_concentration is not None and event_concentration <= 0.25,
        }
        agent["promotion"] = {
            "eligible": all(checks.values()),
            "resolved_positions": len(settled_markets),
            "days_observed": (now - first_trade) / 86400 if agent_trades else 0,
            "categories": len(categories),
            "edge_lcb": edge_lcb,
            "brier_improvement": brier_improvement,
            "max_event_profit_share": event_concentration,
            "checks": checks,
        }

    def book_summary(status: str | None) -> dict[str, Any]:
        selected = [agent for agent in agents if status is None or agent["allocation_status"] == status]
        starting = len(selected) * 10_000
        equity_total = sum(float(agent["equity"]) for agent in selected)
        return {
            "agents": len(selected),
            "aggregate_equity": equity_total,
            "aggregate_starting_cash": starting,
            "return_pct": 100 * (equity_total / starting - 1) if starting else 0,
            "trades": sum(int(agent["trades"]) for agent in selected),
            "fees": sum(float(agent["fees_paid"]) for agent in selected),
            "turnover": sum(float(agent["turnover"]) for agent in selected),
            "realized_pnl": sum(float(agent["realized_pnl"]) for agent in selected),
            "unrealized_pnl": sum(float(agent["unrealized_pnl"]) for agent in selected),
        }

    combined = book_summary(None)
    active_book = book_summary("active")
    shadow_book = book_summary("shadow")

    output = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "epoch": args.epoch,
            "epoch_label": args.label,
            "strategy_version": "v2-edge-only",
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
            "agents_with_positions": sum(1 for agent in agents if agent["positions"] > 0),
            "decision_classes": {
                key: sum(1 for trade in trades if (trade.get("decision_class") or "legacy") == key)
                for key in sorted({str(trade.get("decision_class") or "legacy") for trade in trades})
            },
            "active_book": active_book,
            "shadow_book": shadow_book,
            "combined": combined,
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
