from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass

from .models import Book, FeatureVector, Market, clamp
from .news import NewsSignal


@dataclass(frozen=True)
class PricePoint:
    timestamp: float
    mid: float
    volume_24h: float


class FeatureEngine:
    def __init__(
        self,
        history_size: int = 128,
        initial_history: dict[str, Iterable[PricePoint]] | None = None,
    ) -> None:
        self.history_size = history_size
        self.history: dict[str, deque[PricePoint]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self.appended: list[tuple[str, PricePoint]] = []
        for market_id, points in (initial_history or {}).items():
            self.history[market_id].extend(list(points)[-history_size:])

    def drain_appended(self) -> list[tuple[str, PricePoint]]:
        points = self.appended
        self.appended = []
        return points

    @staticmethod
    def _time_return(points: list[PricePoint], seconds: float) -> tuple[float, bool]:
        if len(points) < 2:
            return 0.0, False
        current = points[-1]
        target = current.timestamp - seconds
        prior = min(points[:-1], key=lambda point: abs(point.timestamp - target))
        tolerance = max(180.0, seconds * 0.55)
        if abs(prior.timestamp - target) > tolerance:
            return 0.0, False
        return current.mid - prior.mid, True

    def build(
        self,
        markets: list[Market],
        books: dict[str, Book],
        news_signals: dict[str, NewsSignal] | None = None,
    ) -> dict[str, FeatureVector]:
        now = time.time()
        usable: dict[str, tuple[Market, Book, Book, float]] = {}
        event_mids: dict[str, list[float]] = defaultdict(list)

        for market in markets:
            binary = market.binary_tokens()
            if binary is None:
                continue
            (_, yes_id), (_, no_id) = binary
            yes_book, no_book = books.get(yes_id), books.get(no_id)
            if not yes_book or not no_book or yes_book.midpoint is None or no_book.midpoint is None:
                continue
            mid = clamp((yes_book.midpoint + (1.0 - no_book.midpoint)) / 2.0, 0.001, 0.999)
            usable[market.id] = (market, yes_book, no_book, mid)
            if market.mutually_exclusive_event:
                event_mids[market.event_id].append(mid)

        output: dict[str, FeatureVector] = {}
        for market_id, (market, yes_book, no_book, mid) in usable.items():
            points_deque = self.history[market_id]
            previous_volume = points_deque[-1].volume_24h if points_deque else market.volume_24h
            timestamp = max(now, yes_book.timestamp or 0.0)
            point = PricePoint(timestamp, mid, market.volume_24h)
            points_deque.append(point)
            self.appended.append((market_id, point))
            points = list(points_deque)
            return_1, ready_1 = self._time_return(points, 5 * 60)
            return_3, ready_3 = self._time_return(points, 15 * 60)
            return_10, ready_10 = self._time_return(points, 50 * 60)
            mids = [point.mid for point in points[-20:]]
            mean = statistics.fmean(mids) if mids else mid
            vol = statistics.pstdev(mids) if len(mids) > 1 else 0.0
            zscore = (mid - mean) / max(vol, 0.005)
            bid_depth = yes_book.depth("bid")
            ask_depth = yes_book.depth("ask")
            imbalance = (bid_depth - ask_depth) / max(1.0, bid_depth + ask_depth)
            event_values = event_mids.get(market.event_id, [])
            event_sum = sum(event_values) if event_values else mid
            event_count = len(event_values)
            normalized = mid / event_sum if event_count > 1 and event_sum > 0 else mid
            yes_ask = yes_book.best_ask if yes_book.best_ask is not None else 1.0
            no_ask = no_book.best_ask if no_book.best_ask is not None else 1.0
            yes_bid = yes_book.best_bid if yes_book.best_bid is not None else 0.0
            no_bid = no_book.best_bid if no_book.best_bid is not None else 0.0
            hours = 24.0 * 365.0
            if market.end_time is not None:
                hours = max(0.0, (market.end_time.timestamp() - now) / 3600.0)
            history_span = max(0.0, points[-1].timestamp - points[0].timestamp) if points else 0.0
            latest_gap = (
                max(0.0, points[-1].timestamp - points[-2].timestamp)
                if len(points) > 1 else float("inf")
            )
            history_ready = (
                len(points) >= 12
                and history_span >= 50 * 60
                and latest_gap <= 15 * 60
                and ready_1 and ready_3 and ready_10
            )
            news = (news_signals or {}).get(market_id, NewsSignal())
            output[market_id] = FeatureVector(
                market=market,
                yes_book=yes_book,
                no_book=no_book,
                mid_yes=mid,
                spread=max(yes_book.spread, no_book.spread),
                imbalance=imbalance,
                return_1=return_1,
                return_3=return_3,
                return_10=return_10,
                rolling_mean=mean,
                zscore=zscore,
                volatility=vol,
                volume_acceleration=(market.volume_24h - previous_volume) / max(100.0, previous_volume),
                hours_to_end=hours,
                event_probability_sum=event_sum,
                event_count=event_count,
                event_residual=mid - normalized,
                complement_buy_edge=1.0 - yes_ask - no_ask,
                complement_sell_edge=yes_bid + no_bid - 1.0,
                observation_count=len(points),
                history_span_seconds=history_span,
                history_ready=history_ready,
                news_relevance=news.relevance,
                news_direction=news.direction,
                news_sources=news.sources,
                news_headlines=news.headlines,
            )
        return output
