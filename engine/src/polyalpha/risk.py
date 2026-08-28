from __future__ import annotations

from dataclasses import dataclass, field

from .models import AgentSpec, FeatureVector, Signal, clamp


@dataclass
class AgentRiskState:
    equity: float
    cash: float
    high_water: float
    active_markets: int = 0
    market_exposure: dict[str, float] = field(default_factory=dict)
    event_exposure: dict[str, float] = field(default_factory=dict)

    @property
    def drawdown(self) -> float:
        return max(0.0, 1.0 - self.equity / max(0.01, self.high_water))


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    target_notional: float
    reason: str


class RiskManager:
    def __init__(
        self,
        max_market_fraction: float = 0.02,
        max_event_fraction: float = 0.08,
        max_active_markets: int = 30,
        throttle_drawdown: float = 0.06,
        kill_drawdown: float = 0.12,
        kelly_multiplier: float = 0.15,
        probation_fraction: float = 0.005,
        max_relative_spread: float = 0.03,
        min_contract_price: float = 0.05,
        max_contract_price: float = 0.95,
    ) -> None:
        self.max_market_fraction = max_market_fraction
        self.max_event_fraction = max_event_fraction
        self.max_active_markets = max_active_markets
        self.throttle_drawdown = throttle_drawdown
        self.kill_drawdown = kill_drawdown
        self.kelly_multiplier = kelly_multiplier
        self.probation_fraction = probation_fraction
        self.max_relative_spread = max_relative_spread
        self.min_contract_price = min_contract_price
        self.max_contract_price = max_contract_price

    def authorize(
        self,
        spec: AgentSpec,
        signal: Signal,
        feature: FeatureVector,
        state: AgentRiskState,
        allocation_multiplier: float = 1.0,
    ) -> RiskDecision:
        existing = state.market_exposure.get(feature.market.id, 0.0)
        if signal.outcome is None:
            return RiskDecision(existing > 0, 0.0, "exit/no edge")
        if state.equity <= 0 or state.drawdown >= self.kill_drawdown:
            return RiskDecision(existing > 0, 0.0, "drawdown kill switch")
        if feature.market.liquidity < spec.min_liquidity and signal.outcome != "BOTH":
            return RiskDecision(existing > 0, 0.0, "liquidity gate")
        if allocation_multiplier <= 0:
            return RiskDecision(existing > 0, 0.0, "adaptive strategy pause")
        if signal.outcome != "BOTH":
            book = feature.yes_book if signal.outcome and signal.outcome.lower() == "yes" else feature.no_book
            bid, ask = book.best_bid, book.best_ask
            if bid is None or ask is None:
                return RiskDecision(existing > 0, 0.0, "incomplete executable book")
            selected_spread = ask - bid
            if selected_spread > spec.max_spread:
                return RiskDecision(existing > 0, 0.0, "selected-spread gate")
            entry_price = ask if signal.execution == "taker" else bid
            if not (self.min_contract_price <= entry_price <= self.max_contract_price):
                return RiskDecision(existing > 0, 0.0, "extreme-price quarantine")
            relative_spread = selected_spread / max(0.01, ask)
            if relative_spread > self.max_relative_spread:
                return RiskDecision(existing > 0, 0.0, "relative-spread gate")
            # Signal.edge is already net of the selected contract's complete
            # modeled entry/exit cost.  Risk must not charge those costs twice.
            if signal.edge < spec.threshold:
                return RiskDecision(existing > 0, 0.0, "net-edge threshold")
        if state.active_markets >= self.max_active_markets and existing <= 0:
            return RiskDecision(False, 0.0, "position-count cap")

        event_value = state.event_exposure.get(feature.market.event_id, 0.0)
        event_room = max(0.0, state.equity * self.max_event_fraction - event_value + existing)
        variance = max(0.01, feature.mid_yes * (1.0 - feature.mid_yes))
        raw_kelly = signal.edge / variance
        if state.drawdown <= self.throttle_drawdown:
            drawdown_throttle = 1.0
        else:
            drawdown_throttle = clamp(
                1.0 - (state.drawdown - self.throttle_drawdown)
                / max(0.001, self.kill_drawdown - self.throttle_drawdown),
                0.0,
                1.0,
            )
        tier_limit = self.probation_fraction if spec.allocation_tier == "probation" else signal.target_fraction
        fraction = min(
            self.max_market_fraction,
            signal.target_fraction,
            tier_limit,
            self.kelly_multiplier * raw_kelly,
        ) * drawdown_throttle * min(1.25, allocation_multiplier)
        target = min(state.equity * fraction, event_room)
        if signal.outcome != "BOTH" and target > 0:
            book = feature.yes_book if signal.outcome and signal.outcome.lower() == "yes" else feature.no_book
            levels = book.asks if signal.execution == "taker" else book.bids
            displayed_notional = levels[0].size * levels[0].price if levels else 0.0
            depth_multiple = 10.0 if signal.execution == "taker" else 2.0
            # Liquidity is a sizing constraint, not an all-or-nothing veto.
            # Preserve the required reserve by reducing the order to the book's
            # supported notional; reject only when even that is below minimum.
            target = min(target, displayed_notional / depth_multiple)
        if target < max(1.0, feature.market.min_order_size * 0.05):
            return RiskDecision(existing > 0, 0.0, "size below risk-adjusted minimum")
        return RiskDecision(True, target, "approved")
