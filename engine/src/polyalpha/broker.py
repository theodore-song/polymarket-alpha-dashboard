from __future__ import annotations

import sqlite3
import math
import time
from dataclasses import dataclass
from pathlib import Path

from .features import PricePoint
from .models import Book, FeatureVector, Market, Signal
from .news import NewsItem
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


@dataclass(frozen=True)
class AdaptationState:
    agent_id: str
    samples: int
    mean_return: float
    lower_bound: float | None
    upper_bound: float | None
    allocation_multiplier: float
    state: str
    win_rate: float = 0.0
    research_samples: int = 0
    executed_samples: int = 0


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
                strategy_version TEXT NOT NULL DEFAULT 'v2.5-validated-alpha',
                horizon INTEGER NOT NULL DEFAULT 1,
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
                retirement_fills INTEGER NOT NULL DEFAULT 0,
                news_items INTEGER NOT NULL DEFAULT 0,
                news_confirmed_markets INTEGER NOT NULL DEFAULT 0,
                news_signal_overlays INTEGER NOT NULL DEFAULT 0,
                history_points INTEGER NOT NULL DEFAULT 0,
                history_ready_markets INTEGER NOT NULL DEFAULT 0,
                signals_generated INTEGER NOT NULL DEFAULT 0,
                executable_signals INTEGER NOT NULL DEFAULT 0,
                signals_approved INTEGER NOT NULL DEFAULT 0,
                risk_rejections INTEGER NOT NULL DEFAULT 0,
                risk_rejection_reasons TEXT NOT NULL DEFAULT '{}',
                counterfactuals_recorded INTEGER NOT NULL DEFAULT 0,
                adaptation_resolved INTEGER NOT NULL DEFAULT 0,
                strategies_paused INTEGER NOT NULL DEFAULT 0,
                active_equity REAL, shadow_equity REAL, combined_equity REAL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS news_items (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
                link TEXT NOT NULL, published_at REAL NOT NULL, fetched_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS adaptation_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL, market_id TEXT NOT NULL, token_id TEXT NOT NULL,
                outcome TEXT NOT NULL, entry_price REAL NOT NULL,
                entry_fee_per_share REAL NOT NULL, fee_rate REAL NOT NULL,
                created_at REAL NOT NULL, due_at REAL NOT NULL,
                resolved_at REAL, future_bid REAL, realized_return REAL,
                strategy_version TEXT NOT NULL,
                evaluation_class TEXT NOT NULL DEFAULT 'executed',
                decision_reason TEXT,
                estimated_probability REAL,
                market_probability REAL,
                net_edge REAL
            );
            CREATE TABLE IF NOT EXISTS strategy_adaptation (
                agent_id TEXT PRIMARY KEY, samples INTEGER NOT NULL DEFAULT 0,
                mean_return REAL NOT NULL DEFAULT 0, m2 REAL NOT NULL DEFAULT 0,
                lower_bound REAL, upper_bound REAL,
                allocation_multiplier REAL NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'warming', updated_at REAL NOT NULL,
                win_rate REAL NOT NULL DEFAULT 0,
                research_samples INTEGER NOT NULL DEFAULT 0,
                executed_samples INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS strategy_epochs (
                strategy_version TEXT PRIMARY KEY, started_at REAL NOT NULL,
                active_equity_baseline REAL NOT NULL, status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS trades_agent_idx ON trades(agent_id, timestamp);
            CREATE INDEX IF NOT EXISTS positions_agent_idx ON positions(agent_id);
            CREATE INDEX IF NOT EXISTS history_market_time_idx ON market_history(market_id,timestamp,id);
            CREATE INDEX IF NOT EXISTS cycles_started_idx ON cycle_runs(started_at);
            CREATE INDEX IF NOT EXISTS news_published_idx ON news_items(published_at);
            CREATE INDEX IF NOT EXISTS adaptation_due_idx ON adaptation_evaluations(resolved_at,due_at);
            CREATE INDEX IF NOT EXISTS idx_adaptation_agent_pending
            ON adaptation_evaluations(agent_id,resolved_at)
            WHERE resolved_at IS NULL;
            """
        )
        additions = {
            "agents": {
                "allocation_status": "TEXT NOT NULL DEFAULT 'active'",
                "allocation_tier": "TEXT NOT NULL DEFAULT 'probation'",
                "strategy_version": "TEXT NOT NULL DEFAULT 'v2.1-continuous'",
                "registered_at": "REAL NOT NULL DEFAULT 0",
                "horizon": "INTEGER NOT NULL DEFAULT 1",
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
            "cycle_runs": {
                "retirement_fills": "INTEGER NOT NULL DEFAULT 0",
                "news_items": "INTEGER NOT NULL DEFAULT 0",
                "news_confirmed_markets": "INTEGER NOT NULL DEFAULT 0",
                "news_signal_overlays": "INTEGER NOT NULL DEFAULT 0",
                "history_points": "INTEGER NOT NULL DEFAULT 0",
                "history_ready_markets": "INTEGER NOT NULL DEFAULT 0",
                "signals_generated": "INTEGER NOT NULL DEFAULT 0",
                "executable_signals": "INTEGER NOT NULL DEFAULT 0",
                "signals_approved": "INTEGER NOT NULL DEFAULT 0",
                "risk_rejections": "INTEGER NOT NULL DEFAULT 0",
                "risk_rejection_reasons": "TEXT NOT NULL DEFAULT '{}'",
                "counterfactuals_recorded": "INTEGER NOT NULL DEFAULT 0",
                "adaptation_resolved": "INTEGER NOT NULL DEFAULT 0",
                "strategies_paused": "INTEGER NOT NULL DEFAULT 0",
            },
            "adaptation_evaluations": {
                "evaluation_class": "TEXT NOT NULL DEFAULT 'executed'",
                "decision_reason": "TEXT",
                "estimated_probability": "REAL",
                "market_probability": "REAL",
                "net_edge": "REAL",
            },
            "strategy_adaptation": {
                "win_rate": "REAL NOT NULL DEFAULT 0",
                "research_samples": "INTEGER NOT NULL DEFAULT 0",
                "executed_samples": "INTEGER NOT NULL DEFAULT 0",
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
        self.db.execute("PRAGMA optimize")
        self.db.commit()

    def ensure_strategy_epoch(self, strategy_version: str) -> bool:
        """Start a clean evidence epoch without rewriting historical trades.

        Old evaluations remain auditable, but they can never authorize capital
        for a new model version.  The reset is performed exactly once per version.
        """
        existing = self.db.execute(
            "SELECT 1 FROM strategy_epochs WHERE strategy_version=?", (strategy_version,)
        ).fetchone()
        if existing is not None:
            return False
        baseline = float(self.db.execute(
            "SELECT COALESCE(SUM(equity),0) FROM agents WHERE allocation_status='active'"
        ).fetchone()[0])
        now = time.time()
        self.db.execute(
            "INSERT INTO strategy_epochs VALUES(?,?,?,'research-only')",
            (strategy_version, now, baseline),
        )
        self.db.execute(
            """UPDATE strategy_adaptation SET samples=0,mean_return=0,m2=0,
               lower_bound=NULL,upper_bound=NULL,allocation_multiplier=0,
               state='research',updated_at=?,win_rate=0,research_samples=0,
               executed_samples=0""",
            (now,),
        )
        self.db.commit()
        return True

    def register_agents(self, specs: list) -> None:
        now = time.time()
        self.db.executemany(
            """INSERT INTO agents
               (id,name,family,allocation_status,allocation_tier,strategy_version,horizon,
                initial_cash,cash,equity,high_water,registered_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, family=excluded.family,
               allocation_status=excluded.allocation_status,
               allocation_tier=excluded.allocation_tier,
               strategy_version=excluded.strategy_version,
               horizon=excluded.horizon""",
            [
                (s.id, s.name, s.family, s.allocation_status, s.allocation_tier,
                 s.strategy_version, s.horizon, self.starting_cash, self.starting_cash,
                 self.starting_cash, self.starting_cash, now, now)
                for s in specs
            ],
        )
        self.db.executemany(
            """INSERT INTO strategy_adaptation(agent_id,updated_at) VALUES(?,?)
               ON CONFLICT(agent_id) DO NOTHING""",
            [(s.id, now) for s in specs],
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
        executed_at = time.time()
        cursor = self.db.execute(
            """INSERT INTO trades(timestamp,agent_id,market_id,token_id,outcome,side,shares,price,fee,execution,reason,
               estimated_yes_probability,market_yes_probability,net_edge,spread,fee_rate,strategy_version,decision_class)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (executed_at, agent_id, market.id, token_id, outcome, side, shares, price, fee,
             execution, reason, estimated_yes_probability, market_yes_probability, net_edge,
             spread, market.fee_rate, self._strategy_version(agent_id), decision_class),
        )
        if side == "BUY" and decision_class == "alpha":
            agent = self.db.execute(
                "SELECT horizon,strategy_version FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            horizon = max(1, int(agent["horizon"] if agent else 1))
            version = str(agent["strategy_version"] if agent else "v2.5-validated-alpha")
            self.db.execute(
                """INSERT INTO adaptation_evaluations(
                   agent_id,market_id,token_id,outcome,entry_price,entry_fee_per_share,
                   fee_rate,created_at,due_at,strategy_version,evaluation_class,
                   decision_reason,estimated_probability,market_probability,net_edge
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    agent_id, market.id, token_id, outcome, price,
                    fee / max(1e-12, shares), market.fee_rate, executed_at,
                    executed_at + horizon * 5 * 60, version, "executed",
                    reason, estimated_yes_probability, market_yes_probability, net_edge,
                ),
            )
        return shares

    def _strategy_version(self, agent_id: str) -> str:
        row = self.db.execute("SELECT strategy_version FROM agents WHERE id=?", (agent_id,)).fetchone()
        return str(row[0]) if row and row[0] else "v2.5-validated-alpha"

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

    def retire_market(self, agent_id: str, f: FeatureVector, reason: str) -> None:
        self._liquidate_market(agent_id, f, reason, decision_class="retirement")
        self.db.commit()

    def cancel_pending(self, agent_id: str) -> None:
        self.db.execute("DELETE FROM pending_orders WHERE agent_id=?", (agent_id,))
        self.db.commit()

    def cancel_stale_pending(self, strategy_version: str) -> int:
        """Remove quotes authored by an older engine before they can fill."""
        cursor = self.db.execute(
            "DELETE FROM pending_orders WHERE COALESCE(strategy_version,'') != ?",
            (strategy_version,),
        )
        self.db.commit()
        return int(cursor.rowcount)

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
            # A resting quote fills only when the opposite executable quote reaches
            # its limit. Same-side quote movement is not evidence of a fill.
            if row["side"] == "BUY":
                crossed = book.best_ask is not None and book.best_ask <= limit
            else:
                crossed = book.best_bid is not None and book.best_bid >= limit
            if crossed:
                opposite_levels = book.asks if row["side"] == "BUY" else book.bids
                available = float(opposite_levels[0].size) if opposite_levels else 0.0
                requested = float(row["shares"])
                fill_shares = min(requested, available)
                executed = self._execute(row["agent_id"], f.market, row["token_id"], row["outcome"], row["side"],
                              fill_shares, limit, "maker", row["reason"],
                              row["estimated_yes_probability"], row["market_yes_probability"],
                              row["net_edge"], row["spread"], row["decision_class"] or "alpha")
                remaining = max(0.0, requested - executed)
                if remaining < 1e-8:
                    self.db.execute("DELETE FROM pending_orders WHERE id=?", (row["id"],))
                else:
                    self.db.execute(
                        "UPDATE pending_orders SET shares=? WHERE id=?",
                        (remaining, row["id"]),
                    )
                fills += int(executed > 0)
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

    def upsert_news(self, items: list[NewsItem], retention_seconds: float = 48 * 3600) -> None:
        self.db.executemany(
            """INSERT INTO news_items(id,source,title,link,published_at,fetched_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               source=excluded.source,title=excluded.title,link=excluded.link,
               published_at=excluded.published_at,fetched_at=excluded.fetched_at""",
            [
                (item.id, item.source, item.title, item.link, item.published_at, item.fetched_at)
                for item in items
            ],
        )
        self.db.execute("DELETE FROM news_items WHERE published_at<?", (time.time() - retention_seconds,))
        self.db.commit()

    def recent_news(self, max_age_seconds: float = 12 * 3600, limit: int = 500) -> list[NewsItem]:
        return [
            NewsItem(
                str(row["id"]), str(row["source"]), str(row["title"]), str(row["link"]),
                float(row["published_at"]), float(row["fetched_at"]),
            )
            for row in self.db.execute(
                """SELECT * FROM news_items WHERE published_at>=?
                   ORDER BY published_at DESC LIMIT ?""",
                (time.time() - max_age_seconds, limit),
            )
        ]

    def record_counterfactual(
        self,
        signal: Signal,
        feature: FeatureVector,
        decision_reason: str,
    ) -> bool:
        """Queue one *eligible* shadow test using conservative taker marks.

        Selection quality is enforced by the engine.  This method deliberately
        refuses complete-set signals: a short-horizon bid liquidation is not a
        valid test of an arbitrage that only realizes $1 at resolution.
        """
        if not signal.preferred_outcome:
            return False
        agent = self.db.execute(
            "SELECT horizon,strategy_version FROM agents WHERE id=?",
            (signal.agent_id,),
        ).fetchone()
        if agent is None:
            return False
        pending = self.db.execute(
            """SELECT 1 FROM adaptation_evaluations WHERE agent_id=? AND strategy_version=?
               AND resolved_at IS NULL LIMIT 1""",
            (signal.agent_id, str(agent["strategy_version"])),
        ).fetchone()
        if pending is not None:
            return False
        binary = feature.market.binary_tokens()
        if binary is None:
            return False
        (yes_outcome, yes_id), (no_outcome, no_id) = binary
        outcome = signal.preferred_outcome
        if outcome.upper() == "BOTH":
            return False
        else:
            is_yes = outcome.lower() == "yes"
            token_id = yes_id if is_yes else no_id
            outcome = yes_outcome if is_yes else no_outcome
            book = feature.yes_book if is_yes else feature.no_book
            if book.best_ask is None:
                return False
            entry_price = float(book.best_ask)
            entry_fee = feature.market.fee_rate * entry_price * (1.0 - entry_price)
        created_at = time.time()
        horizon = max(1, int(agent["horizon"]))
        self.db.execute(
            """INSERT INTO adaptation_evaluations(
               agent_id,market_id,token_id,outcome,entry_price,entry_fee_per_share,
               fee_rate,created_at,due_at,strategy_version,evaluation_class,
               decision_reason,estimated_probability,market_probability,net_edge
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal.agent_id, feature.market.id, token_id, outcome, entry_price,
                entry_fee, feature.market.fee_rate, created_at,
                created_at + horizon * 5 * 60, str(agent["strategy_version"]),
                "eligible-shadow", decision_reason,
                signal.estimated_yes_probability, feature.mid_yes, signal.edge,
            ),
        )
        self.db.commit()
        return True

    def _refresh_adaptation(self, agent_id: str, strategy_version: str) -> None:
        rows = self.db.execute(
            """SELECT realized_return,evaluation_class FROM adaptation_evaluations
               WHERE agent_id=? AND strategy_version=? AND resolved_at IS NOT NULL
               AND evaluation_class IN ('eligible-shadow','executed')""",
            (agent_id, strategy_version),
        ).fetchall()
        values = [float(row["realized_return"]) for row in rows]
        research = sum(row["evaluation_class"] == "eligible-shadow" for row in rows)
        executed = sum(row["evaluation_class"] == "executed" for row in rows)
        samples = len(values)
        mean = sum(values) / samples if samples else 0.0
        m2 = sum((value - mean) ** 2 for value in values)
        lower = upper = None
        if samples >= 2:
            error = math.sqrt(max(0.0, m2 / (samples - 1)) / samples)
            lower, upper = mean - 1.96 * error, mean + 1.96 * error
        win_rate = sum(value > 0 for value in values) / samples if samples else 0.0

        # Capital is opt-in, never the default. Thirty clean forward tests,
        # positive average and lower confidence bound, and >=55% winners are
        # all required. Ten losing live samples revoke the allocation.
        multiplier, state = 0.0, "research"
        if research >= 30:
            if lower is not None and lower > 0 and mean > 0.002 and win_rate >= 0.55:
                multiplier, state = 0.20, "validated"
            else:
                state = "rejected"
        executed_values = [
            float(row["realized_return"]) for row in rows
            if row["evaluation_class"] == "executed"
        ]
        if len(executed_values) >= 10 and sum(executed_values) / len(executed_values) <= 0:
            multiplier, state = 0.0, "paused"
        self.db.execute(
            """INSERT INTO strategy_adaptation(
               agent_id,samples,mean_return,m2,lower_bound,upper_bound,
               allocation_multiplier,state,updated_at,win_rate,research_samples,executed_samples
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET
               samples=excluded.samples,mean_return=excluded.mean_return,m2=excluded.m2,
               lower_bound=excluded.lower_bound,upper_bound=excluded.upper_bound,
               allocation_multiplier=excluded.allocation_multiplier,state=excluded.state,
               updated_at=excluded.updated_at,win_rate=excluded.win_rate,
               research_samples=excluded.research_samples,executed_samples=excluded.executed_samples""",
            (agent_id, samples, mean, m2, lower, upper, multiplier, state,
             time.time(), win_rate, research, executed),
        )

    def refresh_adaptation(self, agent_id: str) -> None:
        """Public test/maintenance hook; always uses the agent's current version."""
        self._refresh_adaptation(agent_id, self._strategy_version(agent_id))
        self.db.commit()

    def resolve_adaptation(self, features: dict[str, FeatureVector]) -> int:
        now = time.time()
        resolved = 0
        rows = self.db.execute(
            """SELECT * FROM adaptation_evaluations
               WHERE resolved_at IS NULL AND due_at<=? ORDER BY due_at,id""",
            (now,),
        ).fetchall()
        for row in rows:
            feature = features.get(str(row["market_id"]))
            if feature is None:
                continue
            binary = feature.market.binary_tokens()
            if binary is None:
                continue
            if str(row["token_id"]) == "BOTH":
                if feature.yes_book.best_bid is None or feature.no_book.best_bid is None:
                    continue
                future_bid = float(feature.yes_book.best_bid + feature.no_book.best_bid)
                exit_fee = float(row["fee_rate"]) * (
                    feature.yes_book.best_bid * (1.0 - feature.yes_book.best_bid)
                    + feature.no_book.best_bid * (1.0 - feature.no_book.best_bid)
                )
            else:
                book = feature.yes_book if row["token_id"] == binary[0][1] else feature.no_book
                if book.best_bid is None:
                    continue
                future_bid = float(book.best_bid)
                exit_fee = float(row["fee_rate"]) * future_bid * (1.0 - future_bid)
            entry_price = float(row["entry_price"])
            realized_return = (
                future_bid - entry_price - float(row["entry_fee_per_share"]) - exit_fee
            ) / max(0.01, entry_price)
            self.db.execute(
                """UPDATE adaptation_evaluations SET resolved_at=?,future_bid=?,realized_return=?
                   WHERE id=?""",
                (now, future_bid, realized_return, row["id"]),
            )
            resolved += 1
        # Recompute from current-version evidence only. This prevents a prior
        # model's contaminated counterfactuals from approving a new strategy.
        for row in self.db.execute("SELECT id,strategy_version FROM agents"):
            self._refresh_adaptation(str(row["id"]), str(row["strategy_version"]))
        self.db.commit()
        return resolved

    def adaptation_states(self) -> dict[str, AdaptationState]:
        return {
            str(row["agent_id"]): AdaptationState(
                str(row["agent_id"]), int(row["samples"]), float(row["mean_return"]),
                float(row["lower_bound"]) if row["lower_bound"] is not None else None,
                float(row["upper_bound"]) if row["upper_bound"] is not None else None,
                float(row["allocation_multiplier"]), str(row["state"]),
                float(row["win_rate"]), int(row["research_samples"]),
                int(row["executed_samples"]),
            )
            for row in self.db.execute("SELECT * FROM strategy_adaptation")
        }

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
        counts = {"alpha": 0, "heartbeat": 0, "maker": 0, "retirement": 0}
        for row in self.db.execute(
            """SELECT decision_class,execution,COUNT(*) AS n FROM trades
               WHERE id>? AND side IN ('BUY','SELL')
               GROUP BY decision_class,execution""",
            (trade_id,),
        ):
            value = int(row["n"])
            if row["decision_class"] == "heartbeat":
                counts["heartbeat"] += value
            elif row["decision_class"] == "alpha":
                counts["alpha"] += value
            elif row["decision_class"] == "retirement":
                counts["retirement"] += value
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
        retirement_fills: int = 0,
        news_items: int = 0,
        news_confirmed_markets: int = 0,
        news_signal_overlays: int = 0,
        history_points: int = 0,
        history_ready_markets: int = 0,
        signals_generated: int = 0,
        executable_signals: int = 0,
        signals_approved: int = 0,
        risk_rejections: int = 0,
        risk_rejection_reasons: str = "{}",
        counterfactuals_recorded: int = 0,
        adaptation_resolved: int = 0,
        strategies_paused: int = 0,
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
               maker_fills=?,retirement_fills=?,news_items=?,news_confirmed_markets=?,
               news_signal_overlays=?,history_points=?,
               history_ready_markets=?,signals_generated=?,executable_signals=?,signals_approved=?,
               risk_rejections=?,risk_rejection_reasons=?,counterfactuals_recorded=?,
               adaptation_resolved=?,strategies_paused=?,
               active_equity=?,shadow_equity=?,combined_equity=?,error=?
               WHERE cycle_id=?""",
            (
                time.time(), status, agents_evaluated, markets_discovered,
                markets_with_books, alpha_fills, heartbeat_fills, maker_fills,
                retirement_fills, news_items, news_confirmed_markets, news_signal_overlays,
                history_points, history_ready_markets,
                signals_generated, executable_signals, signals_approved, risk_rejections,
                risk_rejection_reasons, counterfactuals_recorded, adaptation_resolved,
                strategies_paused,
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
