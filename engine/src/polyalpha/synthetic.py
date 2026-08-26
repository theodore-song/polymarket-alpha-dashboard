from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

from .models import Book, Level, Market, clamp


class SyntheticMarketData:
    """Deterministic exchange used for tests and for the credential-free demo."""

    def __init__(self, market_count: int = 30) -> None:
        self.market_count = market_count
        self.step = 0
        self._markets = self._make_markets(market_count)

    @staticmethod
    def _make_markets(count: int) -> list[Market]:
        markets: list[Market] = []
        for index in range(count):
            event = index // 5
            market_id = f"synthetic-{index:03d}"
            markets.append(
                Market(
                    id=market_id,
                    condition_id=f"condition-{index:03d}",
                    question=f"Will outcome {index % 5 + 1} win synthetic event {event + 1}?",
                    category=("politics", "sports", "crypto", "economics")[event % 4],
                    event_id=f"event-{event:03d}",
                    slug=market_id,
                    end_time=datetime.now(timezone.utc) + timedelta(days=2 + event),
                    liquidity=20_000.0 + index * 500,
                    volume_24h=8_000.0 + index * 250,
                    tokens={"Yes": f"yes-{index:03d}", "No": f"no-{index:03d}"},
                    fee_rate=(0.04, 0.03, 0.07, 0.05)[event % 4],
                    tick_size=0.01,
                    min_order_size=5.0,
                    mutually_exclusive_event=True,
                    active=True,
                    closed=False,
                    accepting_orders=True,
                )
            )
        return markets

    def all_tradable_markets(self, max_markets: int | None = None) -> list[Market]:
        self.step += 1
        markets = self._markets if max_markets is None else self._markets[:max_markets]
        # Refresh volume so attention variants receive a real state change.
        return [
            Market(**{**market.__dict__, "volume_24h": market.volume_24h * (1 + 0.004 * self.step * (1 + (i % 3)))})
            for i, market in enumerate(markets)
        ]

    def _event_probabilities(self, event: int) -> list[float]:
        logits = [
            0.7 * math.sin(0.22 * self.step + event * 0.4 + outcome * 1.1)
            + 0.03 * self.step * (outcome - 2) / 2
            for outcome in range(5)
        ]
        exp = [math.exp(x) for x in logits]
        total = sum(exp)
        return [x / total for x in exp]

    def books(self, token_ids: list[str], batch_size: int = 100) -> dict[str, Book]:
        requested = set(token_ids)
        result: dict[str, Book] = {}
        timestamp = time.time()
        for index, market in enumerate(self._markets):
            probabilities = self._event_probabilities(index // 5)
            yes_mid = probabilities[index % 5]
            # One periodic stale complement creates a known complete-set anomaly.
            complement_offset = -0.018 if self.step % 11 == 0 and index % 13 == 0 else 0.0
            no_mid = clamp(1.0 - yes_mid + complement_offset, 0.01, 0.99)
            spread = 0.012 + 0.004 * ((index + self.step) % 4)
            imbalance = 0.35 * math.sin(self.step * 0.4 + index)
            for token, mid, sign in ((market.tokens["Yes"], yes_mid, 1), (market.tokens["No"], no_mid, -1)):
                if token not in requested:
                    continue
                bid = clamp(mid - spread / 2, 0.001, 0.998)
                ask = clamp(mid + spread / 2, bid + 0.001, 0.999)
                bid_size = 500.0 * (1 + sign * imbalance)
                ask_size = 500.0 * (1 - sign * imbalance)
                result[token] = Book(
                    token,
                    (Level(bid, bid_size), Level(max(0.001, bid - 0.01), 900.0)),
                    (Level(ask, ask_size), Level(min(0.999, ask + 0.01), 900.0)),
                    timestamp,
                )
        return result
