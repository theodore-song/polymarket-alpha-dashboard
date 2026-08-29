from __future__ import annotations

import time
import uuid
import json
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from .agents import StrategyAgent
from .broker import PaperBroker
from .features import FeatureEngine, PricePoint
from .models import Book, FeatureVector, Market
from .news import NewsClient, NewsMatcher
from .risk import RiskManager

STRATEGY_VERSION = "v2.5-validated-alpha"


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
    retirement_fills: int
    news_items: int
    news_confirmed_markets: int
    news_signal_overlays: int
    history_points: int
    history_ready_markets: int
    signals_generated: int
    executable_signals: int
    risk_rejections: int
    risk_rejection_reasons: dict[str, int]
    counterfactuals_recorded: int
    adaptation_resolved: int
    strategies_paused: int
    news_errors: tuple[str, ...]
    elapsed_seconds: float


class TradingEngine:
    def __init__(
        self,
        source: MarketDataSource,
        agents: list[StrategyAgent],
        broker: PaperBroker,
        features: FeatureEngine | None = None,
        risk: RiskManager | None = None,
        news_client: NewsClient | None = None,
        max_new_positions_per_cycle: int = 3,
        max_turnover_fraction: float = 0.10,
        cooldown_seconds: float = 30 * 60,
        validation_required: bool = True,
    ) -> None:
        self.source = source
        self.agents = agents
        self.broker = broker
        self.features = features or FeatureEngine(
            initial_history=broker.load_feature_history()
        )
        self.risk = risk or RiskManager()
        self.news_client = news_client
        self.max_new_positions_per_cycle = max_new_positions_per_cycle
        self.max_turnover_fraction = max_turnover_fraction
        self.cooldown_seconds = cooldown_seconds
        self.validation_required = validation_required
        self.broker.register_agents([agent.spec for agent in agents])
        if validation_required:
            self.broker.ensure_strategy_epoch(STRATEGY_VERSION)

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
        history_points = 0
        history_errors: list[str] = []
        history_getter = getattr(self.source, "price_histories", None)
        if history_getter is not None:
            yes_token_to_market: dict[str, Market] = {}
            for market in markets:
                binary = market.binary_tokens()
                if binary is not None:
                    yes_token_to_market[binary[0][1]] = market
            try:
                raw_histories = history_getter(list(yes_token_to_market))
                backfill = {
                    market.id: [
                        PricePoint(timestamp, price, 0.0)
                        for timestamp, price in raw_histories.get(token_id, [])
                    ]
                    for token_id, market in yes_token_to_market.items()
                    if raw_histories.get(token_id)
                }
                history_points = self.features.backfill(backfill)
            except Exception as exc:
                # Live books still produce a safe cycle. The next chained run can
                # recover history without discarding the last successful ledger.
                history_errors.append(f"price history: {type(exc).__name__}")
        news_errors: list[str] = []
        if self.news_client is not None:
            fetched_news, news_errors = self.news_client.fetch()
            self.broker.upsert_news(fetched_news)
        recent_news = self.broker.recent_news()
        matcher = NewsMatcher(recent_news)
        news_signals = {market.id: matcher.score(market.question) for market in markets}
        news_confirmed_markets = sum(signal.confirmed for signal in news_signals.values())
        feature_map = self.features.build(markets, books, news_signals)
        history_ready_markets = sum(feature.history_ready for feature in feature_map.values())
        self.broker.append_feature_history(self.features.drain_appended())

        # Quotes from older execution models must never fill under v2.5.
        # Crowd-bias quotes are also removed before pending-order processing.
        self.broker.cancel_stale_pending(STRATEGY_VERSION)
        for agent in self.agents:
            if agent.spec.family == "crowd_bias":
                self.broker.cancel_pending(agent.spec.id)
        maker_fills = self.broker.process_pending(feature_map)
        self.broker.mark_to_market(feature_map)
        adaptation_resolved = self.broker.resolve_adaptation(feature_map)
        adaptations = self.broker.adaptation_states()

        # v2.5 keeps the audited retirements in force.
        # All legacy activation/heartbeat inventory and every crowd-bias position
        # is liquidated at the executable bid, with history preserved permanently.
        for agent in self.agents:
            held = self.broker.market_outcomes(agent.spec.id)
            adaptive = adaptations.get(agent.spec.id)
            for market_id in list(held):
                feature = feature_map.get(market_id)
                if feature is None:
                    continue
                origin = self.broker.position_origin_class(agent.spec.id, market_id)
                retire = (
                    agent.spec.family == "crowd_bias"
                    or origin in {"activation", "heartbeat"}
                    or (adaptive is not None and adaptive.state == "paused")
                )
                if retire:
                    self.broker.retire_market(
                        agent.spec.id,
                        feature,
                        "v2.5 audited strategy retirement",
                    )
            if agent.spec.family == "crowd_bias" or (
                adaptive is not None and adaptive.state == "paused"
            ):
                self.broker.cancel_pending(agent.spec.id)

        self.broker.mark_to_market(feature_map)
        states = self.broker.all_risk_states(feature_map)
        approved = 0
        signals_generated = 0
        executable_signals = 0
        risk_rejections = 0
        risk_rejection_reasons: Counter[str] = Counter()
        counterfactuals_recorded = 0
        news_signal_overlays = 0

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

        def eligible_shadow_sample(agent, signals) -> bool:
            """Record only signals that could really have been executed safely.

            This intentionally excludes rejected/extreme ideas, wide books,
            shallow books and complete-set arbitrage.  Those contaminated v2.4's
            learning statistics and made the allocator optimize noise.
            """
            pool = []
            for market_id, signal in signals.items():
                if signal.outcome is None or signal.outcome.upper() == "BOTH":
                    continue
                feature = feature_map[market_id]
                if feature.market.liquidity < max(5_000.0, agent.spec.min_liquidity):
                    continue
                book = feature.yes_book if signal.outcome.lower() == "yes" else feature.no_book
                if book.best_bid is None or book.best_ask is None or not book.asks:
                    continue
                ask = float(book.best_ask)
                spread = ask - float(book.best_bid)
                if not 0.10 <= ask <= 0.90 or spread / max(0.01, ask) > 0.01:
                    continue
                displayed = float(book.asks[0].price * book.asks[0].size)
                if displayed < max(10.0, feature.market.min_order_size * ask * 10.0):
                    continue
                if signal.edge < agent.spec.threshold:
                    continue
                pool.append((signal.edge, feature, signal))
            if not pool:
                return False
            _, feature, signal = max(pool, key=lambda item: item[0])
            return self.broker.record_counterfactual(
                signal, feature, "eligible executable shadow signal"
            )

        for agent in self.agents:
            state = states[agent.spec.id]
            adaptive = adaptations.get(agent.spec.id)
            signals = {market_id: agent.decide(feature) for market_id, feature in feature_map.items()}
            signals_generated += sum(signal.outcome is not None for signal in signals.values())
            news_signal_overlays += sum("external-news overlay" in signal.reason for signal in signals.values())
            if agent.spec.family == "crowd_bias":
                continue
            counterfactuals_recorded += int(eligible_shadow_sample(agent, signals))
            allocation_multiplier = adaptive.allocation_multiplier if adaptive else 0.0
            if not self.validation_required:
                allocation_multiplier = 1.0
            if allocation_multiplier <= 0:
                # Research never stops, but unvalidated hypotheses get no capital.
                continue
            decision_reasons: dict[str, str] = {}
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
                    if signal.edge > 0 and aligned(signal.preferred_outcome, held_outcomes):
                        continue
                    self.broker.rebalance(signal, feature, 0.0, "exit")
                    approved += 1
                    apply_state(state, feature, 0.0)
                    continue

                decision = self.risk.authorize(
                    agent.spec, signal, feature, state, allocation_multiplier
                )
                decision_reasons[market_id] = decision.reason
                if not decision.allowed:
                    risk_rejections += 1
                    risk_rejection_reasons[decision.reason] += 1
                    continue
                target = decision.target_notional
                executable_signals += int(target > 0)
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
                decision = self.risk.authorize(
                    agent.spec, signal, feature, state, allocation_multiplier
                )
                decision_reasons[feature.market.id] = decision.reason
                if not decision.allowed or decision.target_notional <= 0:
                    risk_rejections += 1
                    risk_rejection_reasons[decision.reason] += 1
                    continue
                executable_signals += 1
                if risk_adding_turnover + decision.target_notional > turnover_limit:
                    continue
                self.broker.rebalance(signal, feature, decision.target_notional, "alpha")
                approved += 1
                opened += 1
                risk_adding_turnover += decision.target_notional
                apply_state(state, feature, decision.target_notional)

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
            retirement_fills=fill_counts["retirement"],
            news_items=len(recent_news),
            news_confirmed_markets=news_confirmed_markets,
            news_signal_overlays=news_signal_overlays,
            history_points=history_points,
            history_ready_markets=history_ready_markets,
            signals_generated=signals_generated,
            executable_signals=executable_signals,
            signals_approved=approved,
            risk_rejections=risk_rejections,
            risk_rejection_reasons=json.dumps(dict(risk_rejection_reasons), sort_keys=True),
            counterfactuals_recorded=counterfactuals_recorded,
            adaptation_resolved=adaptation_resolved,
            strategies_paused=sum(state.state in {"paused", "rejected"} for state in adaptations.values()),
            error="; ".join(history_errors + news_errors)[:1000] or None,
        )
        return CycleReport(
            cycle_id,
            len(markets),
            len(feature_map),
            approved,
            fill_counts["maker"],
            fill_counts["alpha"],
            fill_counts["heartbeat"],
            fill_counts["retirement"],
            len(recent_news),
            news_confirmed_markets,
            news_signal_overlays,
            history_points,
            history_ready_markets,
            signals_generated,
            executable_signals,
            risk_rejections,
            dict(risk_rejection_reasons),
            counterfactuals_recorded,
            adaptation_resolved,
            sum(state.state in {"paused", "rejected"} for state in adaptations.values()),
            tuple(history_errors + news_errors),
            time.monotonic() - started,
        )
