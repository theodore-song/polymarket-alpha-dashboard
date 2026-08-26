from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .agents import build_agents
from .api import APIError, PolymarketClient
from .broker import PaperBroker
from .engine import TradingEngine
from .factory import FAMILY_DESCRIPTIONS, build_agent_specs
from .reporting import markdown_performance, write_agent_manifest
from .synthetic import SyntheticMarketData


def default_db() -> Path:
    return Path("data/polyalpha-v2.sqlite3")


def make_broker(path: str, starting_cash: float) -> PaperBroker:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return PaperBroker(db_path, starting_cash=starting_cash)


def cmd_agents(args: argparse.Namespace) -> int:
    specs = build_agent_specs()
    if args.output:
        write_agent_manifest(specs, args.output)
    if args.json:
        print(json.dumps([spec.__dict__ for spec in specs], indent=2))
    else:
        for spec in specs:
            print(f"{spec.id}  {spec.name:<30} {FAMILY_DESCRIPTIONS[spec.family]}")
        print(f"\nTotal: {len(specs)} unique agents")
    return 0


def _print_cycle(number: int, report) -> None:
    print(
        f"cycle={number} id={report.cycle_id} markets={report.markets_discovered} "
        f"books={report.markets_with_books} orders={report.signals_approved} "
        f"alpha_fills={report.alpha_fills} heartbeat_fills={report.heartbeat_fills} "
        f"maker_fills={report.maker_fills} elapsed={report.elapsed_seconds:.2f}s",
        flush=True,
    )


def cmd_demo(args: argparse.Namespace) -> int:
    broker = make_broker(args.db, args.starting_cash)
    try:
        engine = TradingEngine(SyntheticMarketData(args.markets), build_agents(build_agent_specs()), broker)
        for number in range(1, args.cycles + 1):
            _print_cycle(number, engine.cycle(max_markets=args.markets))
        print(markdown_performance(broker, args.top))
        return 0
    finally:
        broker.close()


def cmd_once(args: argparse.Namespace) -> int:
    broker = make_broker(args.db, args.starting_cash)
    try:
        engine = TradingEngine(PolymarketClient(), build_agents(build_agent_specs()), broker)
        _print_cycle(1, engine.cycle(max_markets=args.max_markets))
        print(markdown_performance(broker, args.top))
        return 0
    except APIError as exc:
        print(f"Public Polymarket API error: {exc}", file=sys.stderr)
        return 2
    finally:
        broker.close()


def cmd_run(args: argparse.Namespace) -> int:
    broker = make_broker(args.db, args.starting_cash)
    engine = TradingEngine(PolymarketClient(), build_agents(build_agent_specs()), broker)
    cycle = 0
    try:
        while True:
            cycle += 1
            try:
                _print_cycle(cycle, engine.cycle(max_markets=args.max_markets))
            except APIError as exc:
                print(f"cycle={cycle} data_error={exc}", file=sys.stderr, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped cleanly.")
        return 0
    finally:
        broker.close()


def cmd_report(args: argparse.Namespace) -> int:
    broker = PaperBroker(args.db)
    try:
        report = markdown_performance(broker, args.top)
        if args.output:
            Path(args.output).write_text(report)
        print(report)
        return 0
    finally:
        broker.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="polyalpha", description="100-agent Polymarket paper-trading lab")
    sub = root.add_subparsers(dest="command", required=True)
    agents = sub.add_parser("agents", help="list the exact 100 agent specifications")
    agents.add_argument("--json", action="store_true")
    agents.add_argument("--output", help="write a JSON manifest")
    agents.set_defaults(func=cmd_agents)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--db", default=str(default_db()))
        command.add_argument("--starting-cash", type=float, default=10_000.0)
        command.add_argument("--top", type=int, default=20)

    demo = sub.add_parser("demo", help="run a deterministic offline exchange simulation")
    common(demo)
    demo.add_argument("--cycles", type=int, default=20)
    demo.add_argument("--markets", type=int, default=30)
    demo.set_defaults(func=cmd_demo)

    once = sub.add_parser("once", help="run one cycle against public read-only Polymarket data")
    common(once)
    once.add_argument("--max-markets", type=int)
    once.set_defaults(func=cmd_once)

    run = sub.add_parser("run", help="continuously paper trade public read-only Polymarket data")
    common(run)
    run.add_argument("--max-markets", type=int)
    run.add_argument("--interval", type=float, default=60.0)
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="render a leaderboard from a ledger")
    report.add_argument("--db", default=str(default_db()))
    report.add_argument("--top", type=int, default=20)
    report.add_argument("--output")
    report.set_defaults(func=cmd_report)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
