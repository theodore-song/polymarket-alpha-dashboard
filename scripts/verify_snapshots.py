#!/usr/bin/env python3
"""Verify v1 immutability and v2 book isolation before deployment."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "public/data/epochs/v1-forced-activation.json"
V2 = ROOT / "public/data/snapshot.json"
V1_SHA256 = "416ed15d6b5ee9e56660f5353e7c14eacbd823ecc6f1a5593aeb3075b80fd060"


def main() -> None:
    assert hashlib.sha256(V1.read_bytes()).hexdigest() == V1_SHA256
    snapshot = json.loads(V2.read_text())
    assert snapshot["meta"]["epoch"] == "v2-edge-only"
    assert len(snapshot["agents"]) == 100
    assert sum(agent["allocation_status"] == "active" for agent in snapshot["agents"]) == 90
    assert sum(agent["allocation_status"] == "shadow" for agent in snapshot["agents"]) == 10
    assert snapshot["summary"]["agents_with_trades"] == 100
    assert snapshot["summary"]["agents_with_positions"] == 100
    assert snapshot["summary"]["decision_classes"]["activation"] == 80
    assert all(agent["trades"] > 0 and agent["positions"] > 0 for agent in snapshot["agents"])
    assert not any("discovery position" in trade["reason"] for trade in snapshot["trades"])
    active = snapshot["summary"]["active_book"]
    shadow = snapshot["summary"]["shadow_book"]
    combined = snapshot["summary"]["combined"]
    assert active["agents"] + shadow["agents"] == combined["agents"] == 100
    assert abs(active["aggregate_equity"] + shadow["aggregate_equity"] - combined["aggregate_equity"]) < 1e-6
    print("Snapshot verification passed: immutable v1, isolated books, 100/100 agents trading and positioned.")


if __name__ == "__main__":
    main()
