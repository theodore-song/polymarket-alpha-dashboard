#!/usr/bin/env python3
"""Verify a runtime export before it replaces the public paper ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


V1_SHA256 = "416ed15d6b5ee9e56660f5353e7c14eacbd823ecc6f1a5593aeb3075b80fd060"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    args = parser.parse_args()
    runtime = Path(args.runtime)
    root = Path(__file__).resolve().parents[1]

    v1 = root / "public/data/epochs/v1-forced-activation.json"
    assert hashlib.sha256(v1.read_bytes()).hexdigest() == V1_SHA256

    dashboard = json.loads((runtime / "dashboard.json").read_text())
    trades = json.loads((runtime / "trades.json").read_text())
    equity = json.loads((runtime / "equity.json").read_text())
    health = json.loads((runtime / "health.json").read_text())
    cycle_id = dashboard["meta"]["cycle_id"]

    assert dashboard["meta"]["snapshot_version"] == 2
    assert dashboard["meta"]["epoch"] == "v2-edge-only"
    assert len(dashboard["agents"]) == 100
    assert dashboard["summary"]["agents_evaluated"] == 100
    assert health["status"] == "healthy"
    assert health["meta"]["cycle_id"] == cycle_id
    assert trades["meta"]["cycle_id"] == cycle_id
    assert equity["meta"]["cycle_id"] == cycle_id
    assert len(trades["trades"]) == dashboard["summary"]["trades"]
    assert min(float(agent["cash"]) for agent in dashboard["agents"]) >= 0
    assert sum(agent["allocation_status"] == "active" for agent in dashboard["agents"]) == 90
    assert sum(agent["allocation_status"] == "shadow" for agent in dashboard["agents"]) == 10

    db = sqlite3.connect(runtime / "polyalpha-v2.sqlite3")
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 100
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == len(trades["trades"])
        assert db.execute(
            "SELECT COALESCE(MAX(n),0) FROM (SELECT COUNT(*) n FROM market_history GROUP BY market_id)"
        ).fetchone()[0] <= 128
        cycle = db.execute(
            "SELECT status,agents_evaluated FROM cycle_runs WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
        assert cycle == ("success", 100)
    finally:
        db.close()
    print(f"Runtime verification passed for cycle {cycle_id}.")


if __name__ == "__main__":
    main()
