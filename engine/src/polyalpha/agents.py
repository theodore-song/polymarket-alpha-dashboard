from __future__ import annotations

import math

from .models import AgentSpec, FeatureVector, Signal, clamp


class StrategyAgent:
    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec

    def _estimate_probability(self, f: FeatureVector) -> tuple[float, str]:
        p = f.mid_yes
        speed = self.spec.params["speed"]
        imbalance_weight = self.spec.params["imbalance"]
        family = self.spec.family

        if family == "momentum":
            adjustment = speed * (0.55 * f.return_1 + 0.30 * f.return_3 + 0.15 * f.return_10)
            adjustment += 0.008 * imbalance_weight * f.imbalance
            return p + adjustment, "trend + confirming depth"
        if family == "mean_reversion":
            adjustment = -0.010 * speed * math.tanh(f.zscore / 2.0) - 0.20 * f.return_1
            return p + adjustment, "rolling z-score reversion"
        if family == "orderflow":
            adjustment = 0.014 * imbalance_weight * f.imbalance + 0.15 * f.return_1
            return p + adjustment, "top-five-level order-book pressure"
        if family == "volatility_breakout":
            scale = max(f.volatility, self.spec.params["vol_floor"])
            standardized = f.return_3 / scale
            adjustment = 0.010 * speed * math.tanh(standardized / 2.0)
            return p + adjustment, "volatility-scaled breakout"
        if family == "crowd_bias":
            return p, "crowd-bias model quarantined after calibration audit"
        if family == "attention":
            confirmation = math.tanh(20.0 * f.volume_acceleration)
            adjustment = speed * f.return_3 * confirmation + 0.004 * f.imbalance
            return p + adjustment, "volume-acceleration confirmation"
        if family == "time_catalyst":
            urgency = clamp(72.0 / max(6.0, f.hours_to_end), 0.0, 2.0)
            trend = (0.8 + urgency) * f.return_3
            reversion = -0.006 * (1.0 - min(1.0, urgency)) * math.tanh(f.zscore)
            return p + speed * trend + reversion, "expiry-aware catalyst regime"
        if family == "relative_value":
            if not f.market.mutually_exclusive_event or f.event_count < 3:
                return p, "not an eligible mutually-exclusive event"
            normalized = p / max(0.01, f.event_probability_sum)
            strength = clamp(0.2 + self.spec.params["shrink"], 0.0, 0.7)
            return p + strength * (normalized - p), "negative-risk event normalization"
        if family == "liquidity_maker":
            microprice_shift = 0.25 * f.spread * f.imbalance
            anchor = -0.002 * math.tanh(f.zscore)
            return p + microprice_shift + anchor, "passive microprice fair value"
        return p, "executable complete-set arbitrage"

    @staticmethod
    def _news_adjustment(f: FeatureVector) -> tuple[float, str]:
        """Use independent headlines only when the live book confirms direction."""
        if (
            f.news_sources < 2
            or f.news_relevance < 0.30
            or abs(f.news_direction) < 0.20
        ):
            return 0.0, ""
        market_confirmation = f.return_1 + 0.002 * f.imbalance
        if abs(market_confirmation) < 0.0005 or market_confirmation * f.news_direction <= 0:
            return 0.0, ""
        adjustment = math.copysign(
            min(0.01, 0.0025 + 0.0075 * f.news_relevance),
            f.news_direction,
        )
        return adjustment, (
            f"confirmed external-news overlay ({f.news_headlines} headlines, "
            f"{f.news_sources} sources)"
        )

    @staticmethod
    def _fee_per_share(price: float, fee_rate: float) -> float:
        return fee_rate * price * (1.0 - price)

    def decide(self, f: FeatureVector) -> Signal:
        if self.spec.family == "crowd_bias":
            return Signal(
                self.spec.id, f.market.id, None, f.mid_yes, 0.0, 0.0, 0.0,
                "taker", "crowd-bias model quarantined after calibration audit", None,
            )
        if self.spec.family == "complement_arb":
            fee = self._fee_per_share(f.yes_book.best_ask or 1.0, f.market.fee_rate)
            fee += self._fee_per_share(f.no_book.best_ask or 1.0, f.market.fee_rate)
            edge = f.complement_buy_edge - fee
            outcome = "BOTH" if edge >= self.spec.threshold else None
            return Signal(
                self.spec.id,
                f.market.id,
                outcome,
                f.mid_yes,
                max(0.0, edge),
                clamp(edge / max(0.01, self.spec.threshold * 5), 0.0, 1.0),
                self.spec.risk_fraction,
                "taker",
                "complete-set ask dislocation" if outcome else "no executable complete-set edge",
                "BOTH",
            )

        news_adjustment, news_reason = self._news_adjustment(f)
        history_families = {
            "momentum", "mean_reversion", "orderflow", "volatility_breakout",
            "attention", "time_catalyst",
        }
        if self.spec.family in history_families and not f.history_ready and not news_reason:
            return Signal(
                self.spec.id, f.market.id, None, f.mid_yes, 0.0, 0.0, 0.0,
                self.spec.execution,
                f"warming history ({f.observation_count}/12 time-consistent observations)",
                None,
            )
        if self.spec.family == "liquidity_maker" and f.news_relevance >= 0.30:
            return Signal(
                self.spec.id, f.market.id, None, f.mid_yes, 0.0, 0.0, 0.0,
                self.spec.execution, "news-risk maker pause", None,
            )

        estimated, reason = self._estimate_probability(f)
        if news_reason:
            estimated += news_adjustment
            reason = f"{reason}; {news_reason}"
        estimated = clamp(estimated, 0.005, 0.995)
        if self.spec.execution == "maker":
            yes_cost = f.yes_book.best_bid or 1.0
            no_cost = f.no_book.best_bid or 1.0
            yes_edge = estimated - yes_cost
            no_edge = (1.0 - estimated) - no_cost
        else:
            yes_cost = f.yes_book.best_ask or 1.0
            no_cost = f.no_book.best_ask or 1.0
            yes_edge = estimated - yes_cost - self._fee_per_share(yes_cost, f.market.fee_rate)
            no_edge = (1.0 - estimated) - no_cost - self._fee_per_share(no_cost, f.market.fee_rate)
        edge = max(yes_edge, no_edge)
        preferred_outcome = "Yes" if yes_edge >= no_edge else "No"
        outcome = preferred_outcome
        if edge < self.spec.threshold:
            outcome = None
        confidence = clamp((edge - self.spec.threshold) / 0.08, 0.0, 1.0) if outcome else 0.0
        return Signal(
            agent_id=self.spec.id,
            market_id=f.market.id,
            outcome=outcome,
            estimated_yes_probability=estimated,
            edge=max(0.0, edge),
            confidence=confidence,
            target_fraction=self.spec.risk_fraction * (0.25 + 0.75 * confidence) if outcome else 0.0,
            execution=self.spec.execution,
            reason=reason,
            preferred_outcome=preferred_outcome,
        )

def build_agents(specs: list[AgentSpec]) -> list[StrategyAgent]:
    return [StrategyAgent(spec) for spec in specs]
