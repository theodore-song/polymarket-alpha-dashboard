from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .features import PricePoint
from .models import Book, FeatureVector, Market, Signal
from .risk import AgentRiskState


@dataclass(frozen=True)
class BrokerSummary:
    agent_id: str
    name: str
    family: str
    cash: float
    equity: float
    return_pct: float
    drawdown_pct: float
    trades: int


class PaperBroker:
    """SQLite paper broker. There is intentionally no live-order code path."""

    def __init__(self, path: str | Path, starting_cash: float = 10_000.0) -> None:
        self.path = str(path)
        self.starting_cash = starting_cash
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.db.close()

    def _schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, family TEXT NOT NULL,
                allocation_status TEXT NOT NULL DEFAULT 'active',
                allocation_tier TEXT NOT NULL DEFAULT 'probation',
                strategy_version TEXT NOT NULL DEFAULT 'v2.1-continuous',
                initial_cash REAL NOT NULL, cash REAL NOT NULL,
                equity REAL NOT NULL, high_water REAL NOT NULL,
                registered_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                agent_id TEXT NOT NULL, market_id TEXT NOT NULL, event_id TEXT NOT NULL,
                token_id TEXT NOT NULL, outcome TEXT NOT NULL,
                shares REAL NOT NULL, avg_price REAL NOT NULL,
                PRIMARY KEY(agent_id, market_id, token_id)
            );
            CREATE TABLE IF NOT EXISTS pending_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                market_id TEXT NOT NULL, event_id TEXT NOT NULL, token_id TEXT NOT NULL,
                outcome TEXT NOT NULL, side TEXT NOT NULL, shares REAL NOT NULL,
                limit_price REAL NOT NULL, created_at REAL NOT NULL, reason TEXT NOT NULL,
                estimated_yes_probability REAL, market_yes_probability REAL,
                net_edge REAL, spread REAL, fee_rate REAL,
                strategy_version TEXT, decision_class TEXT
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                agent_id TEXT NOT NULL, market_id TEXT NOT NULL, token_id TEXT NOT NULL,
                outcome TEXT NOT NULL, side TEXT NOT NULL, shares REAL NOT NULL,
                price REAL NOT NULL, fee REAL NOT NULL, execution TEXT NOT NULL,
                reason TEXT NOT NULL, estimated_yes_probability REAL,
                market_yes_probability REAL, net_edge REAL, spread REAL,
                fee_rate REAL, strategy_version TEXT, decision_class TEXT
            );
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                timestamp REAL NOT NULL, agent_id TEXT NOT NULL,
                cash REAL NOT NULL, equity REAL NOT NULL,
                bucket_start INTEGER, reason TEXT NOT NULL DEFAULT 'legacy'
            );
            CREATE TABLE IF NOT EXISTS market_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL, timestamp REAL NOT NULL,
                mid REAL NOT NULL, volume_24h REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cycle_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL UNIQUE,
                started_at REAL NOT NULL, finished_at REAL,
                status TEXT NOT NULL,
                agents_evaluated INTEGER NOT NULL DEFAULT 0,
                markets_discovered INTEGER NOT NULL DEFAULT 0,
                markets_with_books INTEGER NOT NULL DEFAULT 0,
                alpha_fills INTEGER NOT NULL DEFAULT 0,
                heartbeat_fills INTEGER NOT NULL DEFAULT 0,
                maker_fills INTEGER NOT NULL DEFAULT 0,
                active_equity REAL, shadow_equity REAL, combined_equity REAL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS trades_agent_idx ON trades(agent_id, timestamp);
            CREATE INDEX IF NOT EXISTS positions_agent_idx ON positions(agent_id);
            CREATE INDEX IF NOT EXISTS history_market_time_idx ON market_history(market_id,timestamp,id);
            CREATE INDEX IF NOT EXISTS cycles_started_idx ON cycle_runs(started_at);
            """
        )
        additions = {
            "agents": {
                "allocation_status": "TEXT NOT NULL DEFAULT 'active'",
                "allocation_tier": "TEXT NOT NULL DEFAULT 'probation'",
                "strategy_version": "TEXT NOT NULL DEFAULT 'v2.1-continuous'",
                "registered_at": "REAL NOT NULL DEFAULT 0",
            },
            "pending_orders": {
                "estimated_yes_probability": "REAL",
                "market_yes_probability": "REAL",
                "net_edge": "REAL",
                "spread": "REAL",
                "fee_rate": "REAL",
                "strategy_version": "TEXT",
                "decision_class": "TEXT",
            },
            "trades": {
                "estimated_yes_probability": "REAL",
                "market_yes_probability": "REAL",
                "net_edge": "REAL",
                "spread": "REAL",
                "fee_rate": "REAL",
                "strategy_version": "TEXT",
                "decision_class": "TEXT",
            },
            "equity_snapshots": {
                "bucket_start": "INTEGER",
                "reason": "TEXT NOT NULL DEFAULT 'legacy'",
            },
        }
        for table, columns in additions.items():
            existing = {str(row[1]) for row in self.db.execute(f"PRAGMA table_info({table})")}
            for column, declaration in columns.items():
                if column not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        self.db.execute(
            "UPDATE agents SET registered_at=updated_at WHERE registered_at IS NULL OR registered_at=0"
        )
        self.db.commit()

    def register_agents(self, specs: list) -> None:
        now = time.time()
        self.db.executemany(
            """INSERT INTO agents
               (id,name,family,allocation_status,allocation_tier,strategy_version,
                initial_cash,cash,equity,high_water,registered_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, family=excluded.family,
               allocation_status=excluded.allocation_status,
               allocation_tier=excluded.allocation_tier,
               strategy_version=excluded.strategy_version""",
            [
                (s.id, s.name, s.family, s.allocation_status, s.allocation_tier,
                 s.strategy_version, self.starting_cash, self.starting_cash,
                 self.starting_cash, self.starting_cash, now, now)
                for s in specs
            ],
        )
        self.db.commit()

    @staticmethod
    def _fee(shares: float, price: float, rate: float, execution: str) -> float:
        if execution != "taker":
            return 0.0
        return round(max(0.0, shares * rate * price * (1.0 - price)), 5)

    def _cash(self, agent_id: str) -> float:
        row = self.db.execute("SELECT cash FROM agents WHERE id=?", (agent_id,)).fetchone()
        return float(row[0]) if row else 0.0

    def _position(self, agent_id: str, market_id: str, token_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM positions WHERE agent_id=? AND market_id=? AND token_id=?",
            (agent_id, market_id, token_id),
        ).fetchone()

    def _execute(
        self, agent_id: str, market: Market, token_id: str, outcome: str,
        side: str, shares: float, price: float, execution: str, reason: str,
        estimated_yes_probability: float | None = None,
        market_yes_probability: float | None = None,
        net_edge: float | None = None,
        spread: float | None = None,
        decision_class: str = "alpha",
    ) -> float:
        shares = max(0.0, shares)
        if shares < 1e-8 or not (0 < price <= 1):
            return 0.0
        current = self._position(agent_id, market.id, token_id)
        held = float(current["shares"]) if current else 0.0
        if side == "SELL":
            shares = min(shares, held)
        fee = self._fee(shares, price, market.fee_rate, execution)
        cash = self._cash(agent_id)
        if side == "BUY":
            shares = min(shares, max(0.0, cash - fee) / price)
            fee = self._fee(shares, price, market.fee_rate, execution)
            cash_delta = -(shares * price + fee)
            new_shares = held + shares
            old_cost = float(current["avg_price"]) * held if current else 0.0
            avg_price = (old_cost + shares * price) / max(1e-12, new_shares)
        else:
            cash_delta = shares * price - fee
            new_shares = held - shares
            avg_price = float(current["avg_price"]) if current else 0.0
        if shares < 1e-8:
            return 0.0
        self.db.execute("UPDATE agents SET cash=cash+?, updated_at=? WHERE id=?", (cash_delta, time.time(), agent_id))
        if new_shares < 1e-8:
            self.db.execute(
                "DELETE FROM positions WHERE agent_id=? AND market_id=? AND token_id=?",
                (agent_id, market.id, token_id),
            )
        else:
            self.db.execute(
                """INSERT INTO positions(agent_id,market_id,event_id,token_id,outcome,shares,avg_price)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(agent_id,market_id,token_id) DO UPDATE SET
                   shares=excluded.shares, avg_price=excluded.avg_price""",
                (agent_id, market.id, market.event_id, token_id, outcome, new_shares, avg_price),
            )
        self.db.execute(
            """INSERT INTO trades(timestamp,agent_id,market_id,token_id,outcome,side,shares,price,fee,execution,reason,
               estimated_yes_probability,market_yes_probability,net_edge,spread,fee_rate,strategy_version,decision_class)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), agent_id, market.id, token_id, outcome, side, shares, price, fee,
             execution, reason, estimated_yes_probability, market_yes_probability, net_edge,
             spread, market.fee_rate, self._strategy_version(agent_id), decision_class),
        )
        return shares

    def _strategy_version(self, agent_id: str) -> str:
        row = self.db.execute("SELECT strategy_version FROM agents WHERE id=?", (agent_id,)).fetchone()
        return str(row[0]) if row and row[0] else "v2.1-continuous"

    def _liquidate_market(
        self, agent_id: str, f: FeatureVector, reason: str,
        signal: Signal | None = None, decision_class: str = "exit",
    ) -> None:
        binary = f.market.binary_tokens()
        if binary is None:
            return
        books = {binary[0][1]: f.yes_book, binary[1][1]: f.no_book}
        rows = self.db.execute(
            "SELECT * FROM positions WHERE agent_id=? AND market_id=?", (agent_id, f.market.id)
        ).fetchall()
        for row in rows:
            book = books.get(row["token_id"])
            if book and book.best_bid is not None:
                self._execute(agent_id, f.market, row["token_id"], row["outcome"], "SELL",
                              float(row["shares"]), book.best_bid, "taker", reason,
                              signal.estimated_yes_probability if signal else None,
                              f.mid_yes, signal.edge if signal else None, f.spread, decision_class)
        self.db.execute("DELETE FROM pending_orders WHERE agent_id=? AND market_id=?", (agent_id, f.market.id))

    def rebalance(
        self, signal: Signal, f: FeatureVector, target_notional: float,
        decision_class: str = "alpha",
    ) -> None:
        binary = f.market.binary_tokens()
        if binary is None:
            return
        (yes_outcome, yes_id), (no_outcome, no_id) = binary
        if signal.outcome is None or target_notional <= 0:
            self._liquidate_market(signal.agent_id, f, signal.reason, signal, "exit")
            self.db.commit()
            return

        desired = [(yes_outcome, yes_id, f.yes_book), (no_outcome, no_id, f.no_book)] if signal.outcome == "BOTH" else []
        if not desired:
            desired = [(yes_outcome, yes_id, f.yes_book)] if signal.outcome.lower() == "yes" else [(no_outcome, no_id, f.no_book)]
            opposite = no_id if desired[0][1] == yes_id else yes_id
            opposite_book = f.no_book if opposite == no_id else f.yes_book
            row = self._position(signal.agent_id, f.market.id, opposite)
            if row and opposite_book.best_bid is not None:
                self._execute(signal.agent_id, f.market, opposite, row["outcome"], "SELL",
                              float(row["shares"]), opposite_book.best_bid, "taker", "reverse signal",
                              signal.estimated_yes_probability, f.mid_yes, signal.edge, f.spread, "reverse")

        self.db.execute("DELETE FROM pending_orders WHERE agent_id=? AND market_id=?", (signal.agent_id, f.market.id))
        per_leg = target_notional / len(desired)
        for outcome, token_id, book in desired:
            price = book.best_ask if signal.execution == "taker" else book.best_bid
            if price is None:
                continue
            held_row = self._position(signal.agent_id, f.market.id, token_id)
            held = float(held_row["shares"]) if held_row else 0.0
            desired_shares = per_leg / max(0.001, price)
            delta = desired_shares - held
            if abs(delta) * price < max(1.0, f.market.min_order_size * 0.05):
                continue
            if signal.execution == "maker":
                side = "BUY" if delta > 0 else "SELL"
                limit = book.best_bid if side == "BUY" else book.best_ask
                if limit is not None:
                    self.db.execute(
                        """INSERT INTO pending_orders(agent_id,market_id,event_id,token_id,outcome,side,shares,limit_price,created_at,reason,
                           estimated_yes_probability,market_yes_probability,net_edge,spread,fee_rate,strategy_version,decision_class)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (signal.agent_id, f.market.id, f.market.event_id, token_id, outcome, side,
                         abs(delta), limit, time.time(), signal.reason, signal.estimated_yes_probability,
                         f.mid_yes, signal.edge, f.spread, f.market.fee_rate,
                         self._strategy_version(signal.agent_id), decision_class),
                    )
            elif delta > 0 and book.best_ask is not None:
                self._execute(signal.agent_id, f.market, token_id, outcome, "BUY", delta,
                              book.best_ask, "taker", signal.reason, signal.estimated_yes_probability,
                              f.mid_yes, signal.edge, f.spread, decision_class)
            elif delta < 0 and book.best_bid is not None:
                self._execute(signal.agent_id, f.market, token_id, outcome, "SELL", -delta,
                              book.best_bid, "taker", signal.reason, signal.estimated_yes_probability,
                              f.mid_yes, signal.edge, f.spread, decision_class)
        self.db.commit()

    def process_pending(self, features: dict[str, FeatureVector]) -> int:
        rows = self.db.execute("SELECT * FROM pending_orders ORDER BY id").fetchall()
        fills = 0
        for row in rows:
            f = features.get(row["market_id"])
            if not f:
                continue
            binary = f.market.binary_tokens()
            if binary is None:
                continue
            book = f.yes_book if row["token_id"] == binary[0][1] else f.no_book
            limit = float(row["limit_price"])
            # Conservative snapshot fill: the quote must be crossed or the same-side
            # best price must move through it. No rebates/rewards are credited.
            if row["side"] == "BUY":
                crossed = (book.best_ask is not None and book.best_ask <= limit) or (book.best_bid is not None and book.best_bid < limit)
            else:
                crossed = (book.best_bid is not None and book.best_bid >= limit) or (book.best_ask is not None and book.best_ask > limit)
            if crossed:
                self._execute(row["agent_id"], f.market, row["token_id"], row["outcome"], row["side"],
                              float(row["shares"]), limit, "maker", row["reason"],
                              row["estimated_yes_probability"], row["market_yes_probability"],
                              row["net_edge"], row["spread"], row["decision_class"] or "alpha")
                self.db.execute("DELETE FROM pending_orders WHERE id=?", (row["id"],))
                fills += 1
        self.db.commit()
        return fills

    def all_risk_states(self, features: dict[str, FeatureVector]) -> dict[str, AgentRiskState]:
        token_marks: dict[str, float] = {}
        market_events: dict[str, str] = {}
        for f in features.values():
            binary = f.market.binary_tokens()
            if binary:
                token_marks[binary[0][1]] = f.yes_book.best_bid or 0.0
                token_marks[binary[1][1]] = f.no_book.best_bid or 0.0
                market_events[f.market.id] = f.market.event_id
        states: dict[str, AgentRiskState] = {}
        for row in self.db.execute("SELECT * FROM agents"):
            states[row["id"]] = AgentRiskState(float(row["equity"]), float(row["cash"]), float(row["high_water"]))
        for row in self.db.execute("SELECT * FROM positions"):
            state = states.get(row["agent_id"])
            if not state:
                continue
            value = float(row["shares"]) * token_marks.get(row["token_id"], float(row["avg_price"]))
            state.market_exposure[row["market_id"]] = state.market_exposure.get(row["market_id"], 0.0) + value
            event = row["event_id"] or market_events.get(row["market_id"], row["market_id"])
            state.event_exposure[event] = state.event_exposure.get(event, 0.0) + value
        for state in states.values():
            state.active_markets = sum(1 for value in state.market_exposure.values() if value > 0)
            state.equity = state.cash + sum(state.market_exposure.values())
        return states

    def mark_to_market(
        self,
        features: dict[str, FeatureVector],
        record_snapshot: bool = False,
        snapshot_reason: str = "scheduled",
    ) -> None:
        now = time.time()
        states = self.all_risk_states(features)
        for agent_id, state in states.items():
            high_water = max(state.high_water, state.equity)
            self.db.execute(
                "UPDATE agents SET equity=?, high_water=?, updated_at=? WHERE id=?",
                (state.equity, high_water, now, agent_id),
            )
            if record_snapshot:
                bucket = int(now // 3600) * 3600
                self.db.execute(
                    "DELETE FROM equity_snapshots WHERE agent_id=? AND bucket_start=?",
                    (agent_id, bucket),
                )
                self.db.execute(
                    """INSERT INTO equity_snapshots
                       (timestamp,agent_id,cash,equity,bucket_start,reason)
                       VALUES(?,?,?,?,?,?)""",
                    (now, agent_id, state.cash, state.equity, bucket, snapshot_reason),
                )
        self.db.commit()

    def load_feature_history(self, limit: int = 128) -> dict[str, list[PricePoint]]:
        result: dict[str, list[PricePoint]] = {}
        rows = self.db.execute(
            """SELECT market_id,timestamp,mid,volume_24h FROM market_history
               ORDER BY market_id,timestamp,id"""
        )
        for row in rows:
            result.setdefault(str(row["market_id"]), []).append(
                PricePoint(float(row["timestamp"]), float(row["mid"]), float(row["volume_24h"]))
            )
        return {market_id: points[-limit:] for market_id, points in result.items()}

    def append_feature_history(
        self,
        points: list[tuple[str, PricePoint]],
        limit: int = 128,
    ) -> None:
        for market_id, point in points:
            self.db.execute(
                "INSERT INTO market_history(market_id,timestamp,mid,volume_24h) VALUES(?,?,?,?)",
                (market_id, point.timestamp, point.mid, point.volume_24h),
            )
            self.db.execute(
                """DELETE FROM market_history WHERE id IN (
                       SELECT id FROM market_history WHERE market_id=?
                       ORDER BY timestamp DESC,id DESC LIMIT -1 OFFSET ?
                   )""",
                (market_id, limit),
            )
        self.db.commit()

    def last_executed_fill_timestamp(self, agent_id: str) -> float:
        row = self.db.execute(
            """SELECT MAX(timestamp) FROM trades
               WHERE agent_id=? AND side IN ('BUY','SELL')""",
            (agent_id,),
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
        registered = self.db.execute(
            "SELECT registered_at FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        return float(registered[0]) if registered else time.time()

    def decision_class_exposure(self, agent_id: str, decision_class: str) -> float:
        rows = self.db.execute(
            "SELECT market_id,shares,avg_price FROM positions WHERE agent_id=?",
            (agent_id,),
        ).fetchall()
        exposure = 0.0
        for row in rows:
            origin = self.position_origin_class(agent_id, str(row["market_id"]))
            if origin == decision_class:
                exposure += float(row["shares"]) * float(row["avg_price"])
        return exposure

    def latest_trade_id(self) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(id),0) FROM trades").fetchone()
        return int(row[0]) if row else 0

    def fill_counts_since(self, trade_id: int) -> dict[str, int]:
        counts = {"alpha": 0, "heartbeat": 0, "maker": 0}
        for row in self.db.execute(
            """SELECT decision_class,execution,COUNT(*) AS n FROM trades
               WHERE id>? AND side IN ('BUY','SELL')
               GROUP BY decision_class,execution""",
            (trade_id,),
        ):
            value = int(row["n"])
            if row["decision_class"] == "heartbeat":
                counts["heartbeat"] += value
            else:
                counts["alpha"] += value
            if row["execution"] == "maker":
                counts["maker"] += value
        return counts

    def start_cycle(self, cycle_id: str, started_at: float) -> None:
        self.db.execute(
            "INSERT INTO cycle_runs(cycle_id,started_at,status) VALUES(?,?,'running')",
            (cycle_id, started_at),
        )
        self.db.commit()

    def finish_cycle(
        self,
        cycle_id: str,
        *,
        status: str,
        agents_evaluated: int = 0,
        markets_discovered: int = 0,
        markets_with_books: int = 0,
        alpha_fills: int = 0,
        heartbeat_fills: int = 0,
        maker_fills: int = 0,
        error: str | None = None,
    ) -> None:
        books = dict(
            self.db.execute(
                "SELECT allocation_status,SUM(equity) FROM agents GROUP BY allocation_status"
            ).fetchall()
        )
        active = float(books.get("active") or 0.0)
        shadow = float(books.get("shadow") or 0.0)
        self.db.execute(
            """UPDATE cycle_runs SET finished_at=?,status=?,agents_evaluated=?,
               markets_discovered=?,markets_with_books=?,alpha_fills=?,heartbeat_fills=?,
               maker_fills=?,active_equity=?,shadow_equity=?,combined_equity=?,error=?
               WHERE cycle_id=?""",
            (
                time.time(), status, agents_evaluated, markets_discovered,
                markets_with_books, alpha_fills, heartbeat_fills, maker_fills,
                active, shadow, active + shadow, error, cycle_id,
            ),
        )
        self.db.commit()

    def checkpoint(self) -> None:
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def has_market_exposure(self, agent_id: str, market_id: str) -> bool:
        row = self.db.execute(
            """SELECT 1 FROM positions WHERE agent_id=? AND market_id=?
               UNION ALL SELECT 1 FROM pending_orders WHERE agent_id=? AND market_id=? LIMIT 1""",
            (agent_id, market_id, agent_id, market_id),
        ).fetchone()
        return row is not None

    def market_outcomes(self, agent_id: str) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for row in self.db.execute(
            "SELECT market_id,outcome FROM positions WHERE agent_id=?", (agent_id,)
        ):
            result.setdefault(str(row["market_id"]), set()).add(str(row["outcome"]).lower())
        return result

    def position_origin_class(self, agent_id: str, market_id: str) -> str | None:
        row = self.db.execute(
            """SELECT decision_class FROM trades WHERE agent_id=? AND market_id=?
               AND side='BUY' ORDER BY timestamp DESC,id DESC LIMIT 1""",
            (agent_id, market_id),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def last_exit_timestamp(self, agent_id: str, market_id: str) -> float | None:
        row = self.db.execute(
            """SELECT MAX(timestamp) FROM trades WHERE agent_id=? AND market_id=?
               AND side='SELL' AND decision_class IN ('exit','reverse')""",
            (agent_id, market_id),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def open_market_ids(self) -> set[str]:
        return {str(row[0]) for row in self.db.execute("SELECT DISTINCT market_id FROM positions")}

    def settle(self, market: Market) -> int:
        """Settle all virtual positions when Gamma reports an unambiguous 0/1 outcome."""
        if not market.closed or not market.outcome_prices:
            return 0
        rows = self.db.execute("SELECT * FROM positions WHERE market_id=?", (market.id,)).fetchall()
        settled = 0
        for row in rows:
            payout = market.outcome_prices.get(str(row["outcome"]))
            if payout is None or payout not in (0.0, 1.0):
                continue
            shares = float(row["shares"])
            self.db.execute("UPDATE agents SET cash=cash+?, updated_at=? WHERE id=?", (shares * payout, time.time(), row["agent_id"]))
            self.db.execute(
                """INSERT INTO trades(timestamp,agent_id,market_id,token_id,outcome,side,shares,price,fee,execution,reason,
                   estimated_yes_probability,market_yes_probability,net_edge,spread,fee_rate,strategy_version,decision_class)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), row["agent_id"], market.id, row["token_id"], row["outcome"],
                 "SETTLE", shares, payout, 0.0, "resolution", "official market resolution",
                 None, None, None, None, market.fee_rate,
                 self._strategy_version(row["agent_id"]), "resolution"),
            )
            self.db.execute(
                "DELETE FROM positions WHERE agent_id=? AND market_id=? AND token_id=?",
                (row["agent_id"], market.id, row["token_id"]),
            )
            settled += 1
        self.db.commit()
        return settled

    def summaries(self) -> list[BrokerSummary]:
        rows = self.db.execute(
            """SELECT a.*, COUNT(t.id) AS trades FROM agents a
               LEFT JOIN trades t ON t.agent_id=a.id GROUP BY a.id ORDER BY a.equity DESC"""
        ).fetchall()
        return [
            BrokerSummary(
                row["id"], row["name"], row["family"], float(row["cash"]), float(row["equity"]),
                100.0 * (float(row["equity"]) / float(row["initial_cash"]) - 1.0),
                100.0 * (1.0 - float(row["equity"]) / max(0.01, float(row["high_water"]))),
                int(row["trades"]),
            )
            for row in rows
        ]
