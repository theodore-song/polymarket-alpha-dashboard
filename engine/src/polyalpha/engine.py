from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from .agents import StrategyAgent
from .broker import PaperBroker
from .features import FeatureEngine
from .models import Book, FeatureVector, Market
from .risk import RiskManager


class MarketDataSource(Protocol):
    def all_tradable_markets(self, max_markets: int | None = None) -> list[Market]: ...
    def books(self, token_ids: list[str], batch_size: int = 100) -> dict[str, Book]: ...


@dataclass(frozen=True)
class CycleReport:
    cycle_id: str
    markets_discovered: int
    markets_with_books: int
    signals_approved: int
    maker_fills: int
    alpha_fills: int
    heartbeat_fills: int
    elapsed_seconds: float


class TradingEngine:
    def __init__(
        self,
        source: MarketDataSource,
        agents: list[StrategyAgent],
        broker: PaperBroker,
        features: FeatureEngine | None = None,
        risk: RiskManager | None = None,
        max_new_positions_per_cycle: int = 3,
        max_turnover_fraction: float = 0.10,
        cooldown_seconds: float = 30 * 60,
        enable_heartbeat: bool = True,
        heartbeat_interval_seconds: float = 24 * 60 * 60,
    ) -> None:
        self.source = source
        self.agents = agents
        self.broker = broker
        self.features = features or FeatureEngine(
            initial_history=broker.load_feature_history()
        )
        self.risk = risk or RiskManager()
        self.max_new_positions_per_cycle = max_new_positions_per_cycle
        self.max_turnover_fraction = max_turnover_fraction
        self.cooldown_seconds = cooldown_seconds
        self.enable_heartbeat = enable_heartbeat
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.broker.register_agents([agent.spec for agent in agents])

    def cycle(self, max_markets: int | None = None) -> CycleReport:
        cycle_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        cycle_started_at = time.time()
        self.broker.start_cycle(cycle_id, cycle_started_at)
        starting_trade_id = self.broker.latest_trade_id()
        started = time.monotonic()
        try:
            return self._cycle(
                cycle_id, cycle_started_at, starting_trade_id, started, max_markets
            )
        except Exception as exc:
            self.broker.finish_cycle(cycle_id, status="failed", error=str(exc)[:1000])
            raise

    def _cycle(
        self,
        cycle_id: str,
        cycle_started_at: float,
        starting_trade_id: int,
        started: float,
        max_markets: int | None,
    ) -> CycleReport:
        markets = self.source.all_tradable_markets(max_markets=max_markets)
        active_ids = {market.id for market in markets}
        getter = getattr(self.source, "get_market", None)
        if getter is not None:
            for missing_id in self.broker.open_market_ids() - active_ids:
                try:
                    self.broker.settle(getter(missing_id))
                except Exception:
                    # A transient resolution refresh must not prevent market-data processing.
                    pass
        token_ids = [token for market in markets for token in market.tokens.values()]
        books = self.source.books(token_ids)
        feature_map = self.features.build(markets, books)
        self.broker.append_feature_history(self.features.drain_appended())
        maker_fills = self.broker.process_pending(feature_map)
        self.broker.mark_to_market(feature_map)
        states = self.broker.all_risk_states(feature_map)
        approved = 0

        def aligned(outcome: str | None, held: set[str]) -> bool:
            if not outcome:
                return False
            if outcome.lower() == "both":
                return {"yes", "no"}.issubset(held)
            return outcome.lower() in held

        def apply_state(state, feature: FeatureVector, target: float) -> None:
            previous = state.market_exposure.get(feature.market.id, 0.0)
            state.market_exposure[feature.market.id] = target
            event_id = feature.market.event_id
            state.event_exposure[event_id] = max(
                0.0, state.event_exposure.get(event_id, 0.0) - previous + target
            )
            state.active_markets += int(previous <= 0 < target)
            state.active_markets -= int(previous > 0 >= target)

        for agent in self.agents:
            state = states[agent.spec.id]
            signals = {market_id: agent.decide(feature) for market_id, feature in feature_map.items()}
            held = self.broker.market_outcomes(agent.spec.id)
            turnover_limit = max(0.0, state.equity * self.max_turnover_fraction)
            risk_adding_turnover = 0.0

            # Existing inventory uses hysteresis: a position is held while its own
            # side retains positive net edge, even if it no longer clears the entry
            # threshold. Safety exits are never blocked by the turnover budget.
            for market_id, held_outcomes in held.items():
                feature = feature_map.get(market_id)
                signal = signals.get(market_id)
                if feature is None or signal is None:
                    continue
                previous = state.market_exposure.get(market_id, 0.0)
                if signal.outcome is None:
                    if self.broker.position_origin_class(agent.spec.id, market_id) in {
                        "activation", "heartbeat"
                    }:
                        continue
                    if signal.edge > 0 and aligned(signal.preferred_outcome, held_outcomes):
                        continue
                    self.broker.rebalance(signal, feature, 0.0, "exit")
                    approved += 1
                    apply_state(state, feature, 0.0)
                    continue

                decision = self.risk.authorize(agent.spec, signal, feature, state)
                if not decision.allowed:
                    continue
                target = decision.target_notional
                added = max(0.0, target - previous)
                if added > 0 and risk_adding_turnover + added > turnover_limit:
                    continue
                decision_class = "reverse" if not aligned(signal.outcome, held_outcomes) else "alpha"
                self.broker.rebalance(signal, feature, target, decision_class)
                approved += 1
                risk_adding_turnover += added
                apply_state(state, feature, target)

            # New entries are ranked by net executable edge instead of market API
            # order. At most three can be opened in a cycle, and recent exits cool
            # down for 30 minutes unless edge is at least twice the entry threshold.
            candidates = []
            now = time.time()
            for market_id, signal in signals.items():
                if signal.outcome is None or market_id in held:
                    continue
                feature = feature_map[market_id]
                if self.broker.has_market_exposure(agent.spec.id, market_id):
                    continue
                last_exit = self.broker.last_exit_timestamp(agent.spec.id, market_id)
                if (
                    last_exit is not None
                    and now - last_exit < self.cooldown_seconds
                    and signal.edge < 2.0 * agent.spec.threshold
                ):
                    continue
                candidates.append((signal.edge, feature, signal))

            opened = 0
            for _, feature, signal in sorted(candidates, key=lambda item: item[0], reverse=True):
                if opened >= self.max_new_positions_per_cycle:
                    break
                decision = self.risk.authorize(agent.spec, signal, feature, state)
                if not decision.allowed or decision.target_notional <= 0:
                    continue
                if risk_adding_turnover + decision.target_notional > turnover_limit:
                    continue
                self.broker.rebalance(signal, feature, decision.target_notional, "alpha")
                approved += 1
                opened += 1
                risk_adding_turnover += decision.target_notional
                apply_state(state, feature, decision.target_notional)

            # A tiny heartbeat is permitted only after 24 hours with no executed
            # BUY/SELL fill and while strong cash, drawdown, and exposure reserves
            # remain. It is labeled non-alpha and can never satisfy promotion gates.
            inactive_for = time.time() - self.broker.last_executed_fill_timestamp(agent.spec.id)
            if self.enable_heartbeat and inactive_for >= self.heartbeat_interval_seconds:
                actual_state = self.broker.all_risk_states(feature_map)[agent.spec.id]
                gross_exposure = sum(actual_state.market_exposure.values())
                heartbeat_exposure = self.broker.decision_class_exposure(
                    agent.spec.id, "heartbeat"
                )
                reserve_ok = (
                    actual_state.drawdown < 0.06
                    and actual_state.cash >= 0.90 * self.broker.starting_cash
                    and gross_exposure <= 0.10 * actual_state.equity
                    and heartbeat_exposure < 0.005 * actual_state.equity
                )
                heartbeat_candidates = []
                if not reserve_ok:
                    continue
                for feature in feature_map.values():
                    if self.broker.has_market_exposure(agent.spec.id, feature.market.id):
                        continue
                    heartbeat = agent.heartbeat_signal(feature)
                    if heartbeat.edge < -0.0025:
                        continue
                    decision = self.risk.authorize(agent.spec, heartbeat, feature, actual_state)
                    if not decision.allowed or decision.target_notional <= 0:
                        continue
                    selected_book = (
                        feature.yes_book
                        if heartbeat.outcome and heartbeat.outcome.lower() == "yes"
                        else feature.no_book
                    )
                    ask = selected_book.best_ask
                    bid = selected_book.best_bid
                    if ask is None or bid is None or not (0.10 <= ask <= 0.90):
                        continue
                    relative_spread = (ask - bid) / max(0.01, ask)
                    if relative_spread > 0.03:
                        continue
                    displayed_notional = selected_book.asks[0].size * ask if selected_book.asks else 0.0
                    if displayed_notional < 10.0 * decision.target_notional:
                        continue
                    projected_cash = actual_state.cash - 1.01 * decision.target_notional
                    projected_gross = gross_exposure + decision.target_notional
                    projected_heartbeat = heartbeat_exposure + decision.target_notional
                    if (
                        projected_cash < 0.90 * self.broker.starting_cash
                        or projected_gross > 0.10 * actual_state.equity
                        or projected_heartbeat > 0.005 * actual_state.equity
                        or risk_adding_turnover + decision.target_notional > turnover_limit
                    ):
                        continue
                    heartbeat_candidates.append((heartbeat.edge, feature, heartbeat, decision))
                if heartbeat_candidates:
                    _, feature, heartbeat, decision = max(
                        heartbeat_candidates, key=lambda item: item[0]
                    )
                    self.broker.rebalance(
                        heartbeat, feature, decision.target_notional, "heartbeat"
                    )
                    approved += 1
                    risk_adding_turnover += decision.target_notional
        self.broker.mark_to_market(feature_map, record_snapshot=True)
        fill_counts = self.broker.fill_counts_since(starting_trade_id)
        self.broker.finish_cycle(
            cycle_id,
            status="success",
            agents_evaluated=len(self.agents),
            markets_discovered=len(markets),
            markets_with_books=len(feature_map),
            alpha_fills=fill_counts["alpha"],
            heartbeat_fills=fill_counts["heartbeat"],
            maker_fills=fill_counts["maker"],
        )
        return CycleReport(
            cycle_id,
            len(markets),
            len(feature_map),
            approved,
            fill_counts["maker"],
            fill_counts["alpha"],
            fill_counts["heartbeat"],
            time.monotonic() - started,
        )
