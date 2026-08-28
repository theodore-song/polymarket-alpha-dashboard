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


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--trades-output")
    parser.add_argument("--equity-output")
    parser.add_argument("--health-output")
    parser.add_argument("--recent-trades", type=int, default=500)
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
    cycles = rows(db, "SELECT * FROM cycle_runs ORDER BY started_at") if table_exists(db, "cycle_runs") else []
    for cycle in cycles:
        raw_reasons = cycle.get("risk_rejection_reasons") or "{}"
        try:
            cycle["risk_rejection_reasons"] = json.loads(raw_reasons)
        except (TypeError, json.JSONDecodeError):
            cycle["risk_rejection_reasons"] = {}
    successful_cycles = [cycle for cycle in cycles if cycle.get("status") == "success"]
    latest_cycle = successful_cycles[-1] if successful_cycles else None
    latest_attempt = cycles[-1] if cycles else None
    news = rows(db, "SELECT * FROM news_items ORDER BY published_at DESC LIMIT 50") if table_exists(db, "news_items") else []
    adaptation_rows = rows(db, "SELECT * FROM strategy_adaptation ORDER BY agent_id") if table_exists(db, "strategy_adaptation") else []
    adaptations = {row["agent_id"]: row for row in adaptation_rows}
    evaluation_rows = rows(db, "SELECT * FROM adaptation_evaluations") if table_exists(db, "adaptation_evaluations") else []

    trade_counts: dict[str, int] = defaultdict(int)
    position_counts: dict[str, int] = defaultdict(int)
    order_counts: dict[str, int] = defaultdict(int)
    exposure: dict[str, float] = defaultdict(float)
    fees: dict[str, float] = defaultdict(float)
    turnover: dict[str, float] = defaultdict(float)
    decision_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for trade in trades:
        trade_counts[trade["agent_id"]] += 1
        decision_counts[trade["agent_id"]][str(trade.get("decision_class") or "legacy")] += 1
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
            "alpha_trades": decision_counts[agent["id"]].get("alpha", 0),
            "heartbeat_trades": decision_counts[agent["id"]].get("heartbeat", 0),
            "activation_trades": decision_counts[agent["id"]].get("activation", 0),
            "retirement_trades": decision_counts[agent["id"]].get("retirement", 0),
            "adaptation": adaptations.get(agent["id"], {
                "samples": 0, "mean_return": 0, "lower_bound": None,
                "upper_bound": None, "allocation_multiplier": 1, "state": "warming",
            }),
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
        alpha_entries = [
            row for row in agent_trades
            if row["side"] == "BUY"
            and (row.get("decision_class") or "legacy") == "alpha"
        ]
        first_trade = min((float(row["timestamp"]) for row in alpha_entries), default=now)
        alpha_markets = {row["market_id"] for row in alpha_entries}
        settlements = [
            row for row in agent_trades
            if row["side"] == "SETTLE" and row["market_id"] in alpha_markets
        ]
        categories = {market_data.get(row["market_id"], {}).get("category", "Other") for row in settlements}
        resolved_edges: list[float] = []
        model_brier: list[float] = []
        market_brier: list[float] = []
        event_pnl: dict[str, float] = defaultdict(float)
        settled_markets = {row["market_id"] for row in settlements}
        for market_id in settled_markets:
            market_rows = [row for row in agent_trades if row["market_id"] == market_id]
            settlement = next((row for row in reversed(market_rows) if row["side"] == "SETTLE"), None)
            entry = next(
                (
                    row for row in market_rows
                    if row["side"] == "BUY"
                    and (row.get("decision_class") or "legacy") == "alpha"
                ),
                None,
            )
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
        alpha_net_pnl = sum(event_pnl.values())
        event_concentration = (
            max(positive_event_pnl) / sum(positive_event_pnl) if positive_event_pnl else None
        )
        checks = {
            "resolved_sample": len(settled_markets) >= 100,
            "observation_window": (now - first_trade) / 86400 >= 28,
            "category_breadth": len(categories) >= 3,
            "positive_net_return": alpha_net_pnl > 0,
            "positive_edge_lcb": edge_lcb is not None and edge_lcb > 0,
            "calibration_improvement": brier_improvement is not None and brier_improvement > 0,
            "drawdown_control": float(agent["drawdown_pct"]) < 12,
            "event_diversification": event_concentration is not None and event_concentration <= 0.25,
        }
        agent["promotion"] = {
            "eligible": all(checks.values()),
            "resolved_positions": len(settled_markets),
            "days_observed": (now - first_trade) / 86400 if alpha_entries else 0,
            "categories": len(categories),
            "edge_lcb": edge_lcb,
            "brier_improvement": brier_improvement,
            "max_event_profit_share": event_concentration,
            "alpha_net_pnl": alpha_net_pnl,
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
            "alpha_trades": sum(int(agent["alpha_trades"]) for agent in selected),
            "heartbeat_trades": sum(int(agent["heartbeat_trades"]) for agent in selected),
            "activation_trades": sum(int(agent["activation_trades"]) for agent in selected),
            "retirement_trades": sum(int(agent["retirement_trades"]) for agent in selected),
            "fees": sum(float(agent["fees_paid"]) for agent in selected),
            "turnover": sum(float(agent["turnover"]) for agent in selected),
            "realized_pnl": sum(float(agent["realized_pnl"]) for agent in selected),
            "unrealized_pnl": sum(float(agent["unrealized_pnl"]) for agent in selected),
        }

    combined = book_summary(None)
    active_book = book_summary("active")
    shadow_book = book_summary("shadow")

    generated_at = datetime.now(timezone.utc).isoformat()
    cycle_id = str(latest_cycle.get("cycle_id")) if latest_cycle else "bootstrap-static"
    meta = {
            "generated_at": generated_at,
            "snapshot_version": 2,
            "cycle_id": cycle_id,
            "epoch": args.epoch,
            "epoch_label": args.label,
            "strategy_version": "v2.4-executable-learning",
            "mode": "Polymarket public-data paper trading",
            "starting_cash_per_agent": 10_000,
            "currency": "USDC",
            "disclaimer": "Paper-trading research only. Simulated performance is not evidence of future returns.",
    }
    output = {
        "meta": {
            **meta,
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
            "agents_evaluated": int(latest_cycle.get("agents_evaluated") or 0) if latest_cycle else 0,
            "latest_cycle": latest_cycle,
            "active_book": active_book,
            "shadow_book": shadow_book,
            "combined": combined,
            "adaptation": {
                "warming": sum(row.get("state") == "warming" for row in adaptation_rows),
                "probation": sum(row.get("state") == "probation" for row in adaptation_rows),
                "reduced": sum(row.get("state") == "reduced" for row in adaptation_rows),
                "validated": sum(row.get("state") == "validated" for row in adaptation_rows),
                "paused": sum(row.get("state") == "paused" for row in adaptation_rows),
                "evaluations": sum(int(row.get("samples") or 0) for row in adaptation_rows),
                "recorded": len(evaluation_rows),
                "pending": sum(row.get("resolved_at") is None for row in evaluation_rows),
                "counterfactual_recorded": sum(str(row.get("evaluation_class") or "").startswith("counterfactual") for row in evaluation_rows),
                "executed_recorded": sum(row.get("evaluation_class") == "executed" for row in evaluation_rows),
            },
            "news": {
                "items": len(news),
                "sources": len({row["source"] for row in news}),
                "latest_at": max((float(row["published_at"]) for row in news), default=None),
            },
        },
        "agents": agents,
        "trades": trades[: max(0, args.recent_trades)],
        "positions": positions,
        "orders": orders,
        "equity": [
            row for agent_id in {row["agent_id"] for row in equity}
            for row in [item for item in equity if item["agent_id"] == agent_id][-24:]
        ],
        "markets": market_data,
        "news": news,
    }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, separators=(",", ":")) + "\n")
    print(f"Wrote {destination} ({destination.stat().st_size:,} bytes)")
    if args.trades_output:
        trade_destination = Path(args.trades_output)
        trade_destination.parent.mkdir(parents=True, exist_ok=True)
        trade_destination.write_text(
            json.dumps({"meta": meta, "trades": trades}, separators=(",", ":")) + "\n"
        )
        print(f"Wrote {trade_destination} ({trade_destination.stat().st_size:,} bytes)")
    if args.equity_output:
        equity_destination = Path(args.equity_output)
        equity_destination.parent.mkdir(parents=True, exist_ok=True)
        equity_destination.write_text(
            json.dumps({"meta": meta, "equity": equity}, separators=(",", ":")) + "\n"
        )
        print(f"Wrote {equity_destination} ({equity_destination.stat().st_size:,} bytes)")
    if args.health_output:
        last_success = float(latest_cycle.get("finished_at") or 0) if latest_cycle else 0.0
        last_attempt_at = float(
            (latest_attempt or {}).get("finished_at")
            or (latest_attempt or {}).get("started_at")
            or 0
        )
        age_seconds = max(0.0, time.time() - last_success) if last_success else None
        health = {
            "meta": meta,
            "status": "healthy" if age_seconds is not None and age_seconds <= 900 else "stale",
            "last_success_at": last_success or None,
            "last_attempt_at": last_attempt_at or None,
            "next_expected_at": last_success + 300 if last_success else None,
            "age_seconds": age_seconds,
            "cycle": latest_cycle,
            "last_attempt": latest_attempt,
        }
        health_destination = Path(args.health_output)
        health_destination.parent.mkdir(parents=True, exist_ok=True)
        health_destination.write_text(json.dumps(health, separators=(",", ":")) + "\n")
        print(f"Wrote {health_destination} ({health_destination.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
