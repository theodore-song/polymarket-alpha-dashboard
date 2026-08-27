from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


DEFAULT_FEE_RATES = {
    "crypto": 0.07,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
    "world": 0.0,
    "other": 0.05,
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _category(raw: dict[str, Any]) -> str:
    direct = str(raw.get("category") or "").strip().lower()
    if direct:
        return direct
    events = raw.get("events") or []
    if events and isinstance(events[0], dict):
        return str(events[0].get("category") or "other").strip().lower()
    return "other"


def _fee_rate(raw: dict[str, Any], category: str) -> float:
    enabled = bool(raw.get("feesEnabled", raw.get("fees_enabled", False)))
    schedule = raw.get("feeSchedule") or raw.get("fee_schedule") or {}
    if isinstance(schedule, dict):
        for key in ("rate", "feeRate", "fee_rate", "r"):
            if key in schedule:
                return max(0.0, safe_float(schedule[key]))
    explicit = safe_float(raw.get("takerBaseFee", raw.get("taker_base_fee", 0)))
    if explicit > 0:
        return explicit / 10_000 if explicit > 1 else explicit
    if not enabled:
        return 0.0
    for key, rate in DEFAULT_FEE_RATES.items():
        if key in category:
            return rate
    return DEFAULT_FEE_RATES["other"]


@dataclass(frozen=True)
class Market:
    id: str
    condition_id: str
    question: str
    category: str
    event_id: str
    slug: str
    end_time: datetime | None
    liquidity: float
    volume_24h: float
    tokens: dict[str, str]
    fee_rate: float
    tick_size: float
    min_order_size: float
    mutually_exclusive_event: bool
    active: bool
    closed: bool
    accepting_orders: bool
    outcome_prices: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_gamma(cls, raw: dict[str, Any]) -> "Market":
        outcomes = [str(x) for x in json_list(raw.get("outcomes"))]
        token_ids = [str(x) for x in json_list(raw.get("clobTokenIds"))]
        tokens = dict(zip(outcomes, token_ids, strict=False))
        price_values = json_list(raw.get("outcomePrices"))
        outcome_prices = {
            outcome: safe_float(price)
            for outcome, price in zip(outcomes, price_values, strict=False)
        }
        events = raw.get("events") or []
        event = events[0] if events and isinstance(events[0], dict) else {}
        category = _category(raw)
        return cls(
            id=str(raw.get("id") or raw.get("conditionId") or ""),
            condition_id=str(raw.get("conditionId") or ""),
            question=str(raw.get("question") or ""),
            category=category,
            event_id=str(event.get("id") or raw.get("eventId") or raw.get("id") or ""),
            slug=str(raw.get("slug") or ""),
            end_time=parse_time(raw.get("endDate") or raw.get("endDateIso")),
            liquidity=safe_float(raw.get("liquidityNum", raw.get("liquidity"))),
            volume_24h=safe_float(raw.get("volume24hr", raw.get("volume24hrClob"))),
            tokens=tokens,
            fee_rate=_fee_rate(raw, category),
            tick_size=max(0.0001, safe_float(raw.get("orderPriceMinTickSize"), 0.01)),
            min_order_size=max(0.0, safe_float(raw.get("orderMinSize"), 5.0)),
            mutually_exclusive_event=bool(event.get("negRisk", raw.get("negRisk", False))),
            active=bool(raw.get("active", True)),
            closed=bool(raw.get("closed", False)),
            accepting_orders=bool(raw.get("acceptingOrders", True)),
            outcome_prices=outcome_prices,
        )

    def binary_tokens(self) -> tuple[tuple[str, str], tuple[str, str]] | None:
        if len(self.tokens) != 2:
            return None
        items = list(self.tokens.items())
        yes = next((item for item in items if item[0].lower() == "yes"), items[0])
        no = next((item for item in items if item[0].lower() == "no"), items[1])
        return yes, no


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    asset_id: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    timestamp: float

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Book":
        def levels(values: Iterable[dict[str, Any]], reverse: bool) -> tuple[Level, ...]:
            parsed = [Level(safe_float(x.get("price")), safe_float(x.get("size"))) for x in values]
            parsed = [x for x in parsed if 0 < x.price < 1 and x.size > 0]
            return tuple(sorted(parsed, key=lambda x: x.price, reverse=reverse))

        ts = safe_float(raw.get("timestamp"))
        if ts > 10_000_000_000:
            ts /= 1000
        return cls(
            asset_id=str(raw.get("asset_id") or raw.get("assetId") or raw.get("token_id") or ""),
            bids=levels(raw.get("bids") or [], True),
            asks=levels(raw.get("asks") or [], False),
            timestamp=ts,
        )

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        if self.best_bid is None or self.best_ask is None:
            return 1.0
        return max(0.0, self.best_ask - self.best_bid)

    def depth(self, side: str, levels: int = 5) -> float:
        source = self.bids if side == "bid" else self.asks
        return sum(level.size for level in source[:levels])


@dataclass(frozen=True)
class FeatureVector:
    market: Market
    yes_book: Book
    no_book: Book
    mid_yes: float
    spread: float
    imbalance: float
    return_1: float
    return_3: float
    return_10: float
    rolling_mean: float
    zscore: float
    volatility: float
    volume_acceleration: float
    hours_to_end: float
    event_probability_sum: float
    event_count: int
    event_residual: float
    complement_buy_edge: float
    complement_sell_edge: float
    observation_count: int = 0
    history_span_seconds: float = 0.0
    history_ready: bool = False
    news_relevance: float = 0.0
    news_direction: float = 0.0
    news_sources: int = 0
    news_headlines: int = 0


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    family: str
    variant: int
    threshold: float
    horizon: int
    risk_fraction: float
    max_spread: float
    min_liquidity: float
    execution: str
    params: dict[str, float]
    allocation_status: str = "active"
    allocation_tier: str = "probation"
    strategy_version: str = "v2.2-adaptive-news"


@dataclass(frozen=True)
class Signal:
    agent_id: str
    market_id: str
    outcome: str | None
    estimated_yes_probability: float
    edge: float
    confidence: float
    target_fraction: float
    execution: str
    reason: str
    preferred_outcome: str | None = None
    signal_kind: str = "alpha"
