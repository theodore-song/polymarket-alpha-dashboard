from __future__ import annotations

import json
from pathlib import Path

from .broker import PaperBroker
from .factory import FAMILY_DESCRIPTIONS
from .models import AgentSpec


def write_agent_manifest(specs: list[AgentSpec], path: str | Path) -> None:
    rows = []
    for spec in specs:
        row = dict(spec.__dict__)
        row["strategy"] = FAMILY_DESCRIPTIONS[spec.family]
        rows.append(row)
    Path(path).write_text(json.dumps(rows, indent=2) + "\n")


def markdown_performance(broker: PaperBroker, limit: int = 20) -> str:
    summaries = broker.summaries()
    lines = [
        "# PolyAlpha paper-trading leaderboard",
        "",
        "Liquidation-value accounting; simulated performance is not evidence of future returns.",
        "",
        "| Rank | Agent | Family | Equity | Return | Drawdown | Trades |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(summaries[:limit], 1):
        lines.append(
            f"| {rank} | {row.agent_id} {row.name} | {row.family} | ${row.equity:,.2f} | "
            f"{row.return_pct:+.2f}% | {row.drawdown_pct:.2f}% | {row.trades} |"
        )
    return "\n".join(lines) + "\n"
