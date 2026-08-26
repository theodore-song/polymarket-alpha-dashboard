#!/usr/bin/env python3
"""Bootstrap the durable v2 SQLite ledger from the checked-in public snapshot."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from polyalpha.broker import PaperBroker


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def insert_rows(broker: PaperBroker, table: str, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    columns = {str(row[1]) for row in broker.db.execute(f"PRAGMA table_info({table})")}
    for record in records:
        selected = {key: value for key, value in record.items() if key in columns}
        names = list(selected)
        placeholders = ",".join("?" for _ in names)
        broker.db.execute(
            f"INSERT INTO {table}({','.join(names)}) VALUES({placeholders})",
            tuple(selected[name] for name in names),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    snapshot = json.loads(Path(args.snapshot).read_text())
    destination = Path(args.db)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise SystemExit(f"Refusing to overwrite existing ledger: {destination}")

    generated_at = timestamp(snapshot["meta"]["generated_at"])
    broker = PaperBroker(destination)
    try:
        for table in ("agents", "positions", "pending_orders", "trades", "equity_snapshots"):
            broker.db.execute(f"DELETE FROM {table}")

        trades_by_agent: dict[str, list[dict[str, Any]]] = {}
        for trade in snapshot.get("trades", []):
            trades_by_agent.setdefault(str(trade["agent_id"]), []).append(trade)

        for agent in snapshot["agents"]:
            equity = float(agent["equity"])
            drawdown = max(0.0, float(agent.get("drawdown_pct") or 0) / 100.0)
            high_water = equity / max(0.0001, 1.0 - drawdown)
            fills = [
                float(row["timestamp"])
                for row in trades_by_agent.get(str(agent["id"]), [])
                if row.get("side") in {"BUY", "SELL"}
            ]
            registered_at = min(fills, default=generated_at)
            broker.db.execute(
                """INSERT INTO agents(
                   id,name,family,allocation_status,allocation_tier,strategy_version,
                   initial_cash,cash,equity,high_water,registered_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    agent["id"], agent["name"], agent["family"],
                    agent.get("allocation_status", "active"),
                    agent.get("allocation_tier", "probation"),
                    agent.get("strategy_version", "v2-edge-only"),
                    snapshot["meta"].get("starting_cash_per_agent", 10_000),
                    agent["cash"], equity, high_water, registered_at, generated_at,
                ),
            )

        insert_rows(broker, "positions", snapshot.get("positions", []))
        insert_rows(broker, "pending_orders", snapshot.get("orders", []))
        insert_rows(broker, "trades", snapshot.get("trades", []))
        insert_rows(broker, "equity_snapshots", snapshot.get("equity", []))
        broker.db.commit()

        counts = broker.db.execute(
            """SELECT
               (SELECT COUNT(*) FROM agents),
               (SELECT COUNT(*) FROM trades),
               (SELECT COUNT(*) FROM positions)"""
        ).fetchone()
        expected = (
            len(snapshot["agents"]),
            len(snapshot.get("trades", [])),
            len(snapshot.get("positions", [])),
        )
        if tuple(counts) != expected:
            raise RuntimeError(f"Bootstrap count mismatch: {tuple(counts)} != {expected}")
        broker.checkpoint()
        print(
            f"Bootstrapped {counts[0]} agents, {counts[1]} trades, "
            f"and {counts[2]} positions into {destination}"
        )
    finally:
        broker.close()


if __name__ == "__main__":
    main()
