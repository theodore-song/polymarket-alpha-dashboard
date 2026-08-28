from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polyalpha.agents import StrategyAgent, build_agents
from polyalpha.api import PolymarketClient
from polyalpha.broker import PaperBroker
from polyalpha.engine import TradingEngine
from polyalpha.factory import build_agent_specs
from polyalpha.features import FeatureEngine, PricePoint
from polyalpha.models import Book, Level, Market, Signal
from polyalpha.news import NewsItem, NewsMatcher
from polyalpha.risk import AgentRiskState, RiskManager
from polyalpha.synthetic import SyntheticMarketData


class FactoryTests(unittest.TestCase):
    def test_exactly_100_unique_agents(self) -> None:
        specs = build_agent_specs()
        self.assertEqual(len(specs), 100)
        self.assertEqual(len({spec.id for spec in specs}), 100)
        self.assertEqual(len({(spec.family, spec.variant) for spec in specs}), 100)
        self.assertEqual(sum(spec.allocation_status == "active" for spec in specs), 90)
        self.assertEqual(sum(spec.allocation_status == "shadow" for spec in specs), 10)
        self.assertTrue(all(spec.allocation_tier == "probation" for spec in specs))
        self.assertAlmostEqual(min(spec.risk_fraction for spec in specs), 0.005)
        self.assertAlmostEqual(max(spec.risk_fraction for spec in specs), 0.0125)

    def test_gamma_string_arrays_are_parsed(self) -> None:
        market = Market.from_gamma({
            "id": "7", "conditionId": "0x7", "question": "Test?", "category": "Politics",
            "outcomes": '["Yes", "No"]', "clobTokenIds": '["yes-token", "no-token"]',
            "outcomePrices": '["0.61", "0.39"]', "active": True, "closed": False,
            "acceptingOrders": True, "feesEnabled": True,
        })
        self.assertEqual(market.tokens["Yes"], "yes-token")
        self.assertAlmostEqual(market.outcome_prices["No"], 0.39)
        self.assertAlmostEqual(market.fee_rate, 0.04)

    def test_documented_price_history_is_batched_and_validated(self) -> None:
        class FakeHTTP:
            def __init__(self):
                self.payloads = []

            def request(self, method, url, payload=None):
                self.payloads.append(payload)
                return {"history": {
                    token: [{"t": 100, "p": .4}, {"t": 200, "p": .5}, {"t": 0, "p": 2}]
                    for token in payload["markets"]
                }}

        http = FakeHTTP()
        history = PolymarketClient(http=http).price_histories(
            [f"token-{index}" for index in range(21)]
        )
        self.assertEqual(len(http.payloads), 2)
        self.assertTrue(all(len(payload["markets"]) <= 20 for payload in http.payloads))
        self.assertEqual(history["token-0"], [(100.0, .4), (200.0, .5)])

    def test_runtime_workflow_self_chains_instead_of_trusting_five_minute_cron(self) -> None:
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github/workflows/paper-cycle.yml").read_text()
        self.assertIn("actions: write", workflow)
        self.assertIn("gh workflow run paper-cycle.yml", workflow)
        self.assertIn("300 - elapsed", workflow)
        self.assertNotIn("2,7,12,17,22,27,32,37,42,47,52,57", workflow)


class FeatureAndAgentTests(unittest.TestCase):
    def make_market(self) -> Market:
        return Market(
            "m1", "c1", "Outcome?", "politics", "event", "m1",
            datetime.now(timezone.utc) + timedelta(days=1), 50_000, 10_000,
            {"Yes": "yes", "No": "no"}, 0.0, 0.01, 5, True, True, False, True,
        )

    def test_complement_edge_uses_executable_asks(self) -> None:
        market = self.make_market()
        books = {
            "yes": Book("yes", (Level(.48, 100),), (Level(.49, 100),), 1),
            "no": Book("no", (Level(.48, 100),), (Level(.49, 100),), 1),
        }
        f = FeatureEngine().build([market], books)["m1"]
        self.assertAlmostEqual(f.complement_buy_edge, 0.02)
        agent = StrategyAgent(build_agent_specs()[80])
        self.assertEqual(agent.decide(f).outcome, "BOTH")

    def test_temporal_history_requires_real_elapsed_time(self) -> None:
        market = self.make_market()
        now = time.time()
        books = {
            "yes": Book("yes", (Level(.49, 1000),), (Level(.50, 1000),), now),
            "no": Book("no", (Level(.49, 1000),), (Level(.50, 1000),), now),
        }
        regular = [PricePoint(now - (11 - index) * 300, .45 + index * .004, 1000) for index in range(12)]
        ready = FeatureEngine(initial_history={"m1": regular}).build([market], books)["m1"]
        stale = [PricePoint(now - 7200 + index * 300, .45 + index * .004, 1000) for index in range(12)]
        not_ready = FeatureEngine(initial_history={"m1": stale}).build([market], books)["m1"]
        self.assertTrue(ready.history_ready)
        self.assertFalse(not_ready.history_ready)

    def test_authoritative_backfill_unfreezes_sparse_runner_history(self) -> None:
        market = self.make_market()
        now = time.time()
        sparse = [PricePoint(now - 12 * 3600, .40, 900), PricePoint(now - 6 * 3600, .42, 950)]
        regular = [PricePoint(now - (12 - index) * 300, .43 + index * .002, 0) for index in range(12)]
        features = FeatureEngine(initial_history={"m1": sparse})
        self.assertEqual(features.backfill({"m1": regular}), 12)
        self.assertEqual(features.backfill({"m1": regular}), 0)
        vector = features.build([market], {
            "yes": Book("yes", (Level(.45, 1000),), (Level(.46, 1000),), now),
            "no": Book("no", (Level(.54, 1000),), (Level(.55, 1000),), now),
        })["m1"]
        self.assertTrue(vector.history_ready)
        self.assertGreaterEqual(vector.observation_count, 12)

    def test_news_requires_two_independent_sources(self) -> None:
        now = time.time()
        one = NewsItem("1", "Source A", "Candidate Smith wins confirmed vote", "https://a.test", now, now)
        two = NewsItem("2", "Source B", "Candidate Smith victory confirmed", "https://b.test", now, now)
        self.assertFalse(NewsMatcher([one]).score("Will Candidate Smith win?").confirmed)
        signal = NewsMatcher([one, two]).score("Will Candidate Smith win?")
        self.assertTrue(signal.confirmed)
        self.assertGreater(signal.direction, 0)


class BrokerAndEngineTests(unittest.TestCase):
    @staticmethod
    def single_market_data():
        market = Market(
            "single", "single-condition", "Signal lifecycle?", "other", "single-event", "single",
            datetime.now(timezone.utc) + timedelta(days=2), 50_000, 10_000,
            {"Yes": "single-yes", "No": "single-no"}, 0.0, 0.01, 5,
            True, True, False, True,
        )

        class SingleMarketData:
            def all_tradable_markets(self, max_markets=None):
                return [market]

            def books(self, token_ids, batch_size=100):
                return {
                    "single-yes": Book("single-yes", (Level(.49, 1000),), (Level(.50, 1000),), 1),
                    "single-no": Book("single-no", (Level(.49, 1000),), (Level(.50, 1000),), 1),
                }

        return SingleMarketData()

    def test_demo_runs_and_never_creates_negative_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = PaperBroker(Path(tmp) / "paper.sqlite3")
            engine = TradingEngine(SyntheticMarketData(10), build_agents(build_agent_specs()), broker)
            for _ in range(4):
                report = engine.cycle(max_markets=10)
            self.assertEqual(report.markets_with_books, 10)
            self.assertEqual(len(broker.summaries()), 100)
            self.assertGreaterEqual(min(row.cash for row in broker.summaries()), 0.0)
            broker.close()

    def test_no_edge_stays_flat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = PaperBroker(Path(tmp) / "paper.sqlite3")
            market = Market(
                "flat", "flat-condition", "No edge?", "other", "flat-event", "flat",
                datetime.now(timezone.utc) + timedelta(days=2), 50_000, 10_000,
                {"Yes": "flat-yes", "No": "flat-no"}, 0.05, 0.01, 5,
                True, True, False, True,
            )

            class NoEdgeData:
                def all_tradable_markets(self, max_markets=None):
                    return [market]

                def books(self, token_ids, batch_size=100):
                    return {
                        "flat-yes": Book("flat-yes", (Level(.499, 1000),), (Level(.50, 1000),), 1),
                        "flat-no": Book("flat-no", (Level(.499, 1000),), (Level(.50, 1000),), 1),
                    }

            try:
                engine = TradingEngine(NoEdgeData(), build_agents(build_agent_specs()), broker)
                engine.cycle()
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
            finally:
                broker.close()

    def test_heartbeat_is_disabled_even_after_24_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = PaperBroker(Path(tmp) / "paper.sqlite3")
            source = self.single_market_data()
            spec = replace(
                build_agent_specs()[0], min_liquidity=0, max_spread=1,
            )
            agent = StrategyAgent(spec)
            try:
                engine = TradingEngine(source, [agent], broker)
                broker.db.execute(
                    "UPDATE agents SET registered_at=? WHERE id=?",
                    ((datetime.now(timezone.utc) - timedelta(hours=25)).timestamp(), spec.id),
                )
                broker.db.commit()
                first = engine.cycle()
                self.assertEqual(first.heartbeat_fills, 0)
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
                second = engine.cycle()
                self.assertEqual(second.heartbeat_fills, 0)
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
            finally:
                broker.close()

    def test_relative_spread_gate_rejects_cheap_wide_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = PaperBroker(Path(tmp) / "paper.sqlite3")
            spec = replace(build_agent_specs()[0], min_liquidity=0, max_spread=1)
            try:
                market = FeatureAndAgentTests().make_market()
                feature = FeatureEngine().build([market], {
                    "yes": Book("yes", (Level(.035, 10000),), (Level(.059, 10000),), time.time()),
                    "no": Book("no", (Level(.941, 10000),), (Level(.965, 10000),), time.time()),
                })["m1"]
                signal = Signal(spec.id, "m1", "Yes", .20, .12, 1, .01, "taker", "test", "Yes")
                decision = RiskManager().authorize(
                    spec, signal, feature, AgentRiskState(10_000, 10_000, 10_000)
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "relative-spread gate")
            finally:
                broker.close()

    def test_selected_contract_is_not_blocked_by_opposite_book_spread(self) -> None:
        market = replace(FeatureAndAgentTests().make_market(), fee_rate=0)
        feature = FeatureEngine().build([market], {
            "yes": Book("yes", (Level(.49, 2000),), (Level(.50, 2000),), time.time()),
            "no": Book("no", (Level(.40, 2000),), (Level(.50, 2000),), time.time()),
        })["m1"]
        spec = replace(build_agent_specs()[0], min_liquidity=0, max_spread=.04)
        signal = Signal(spec.id, "m1", "Yes", .60, .08, 1, .01, "taker", "test", "Yes")
        decision = RiskManager().authorize(
            spec, signal, feature, AgentRiskState(10_000, 10_000, 10_000)
        )
        self.assertTrue(decision.allowed)

    def test_canonical_net_edge_is_not_charged_twice(self) -> None:
        market = replace(FeatureAndAgentTests().make_market(), fee_rate=.04)
        feature = FeatureEngine().build([market], {
            "yes": Book("yes", (Level(.49, 2000),), (Level(.50, 2000),), time.time()),
            "no": Book("no", (Level(.49, 2000),), (Level(.50, 2000),), time.time()),
        })["m1"]
        spec = replace(build_agent_specs()[0], threshold=.002, min_liquidity=0)
        signal = Signal(spec.id, "m1", "Yes", .54, .003, .2, .005,
                        "taker", "fully after-cost test", "Yes", .03, .52)
        decision = RiskManager().authorize(
            spec, signal, feature, AgentRiskState(10_000, 10_000, 10_000)
        )
        self.assertTrue(decision.allowed)

    def test_displayed_depth_caps_size_instead_of_vetoing_trade(self) -> None:
        market = replace(FeatureAndAgentTests().make_market(), fee_rate=0)
        feature = FeatureEngine().build([market], {
            "yes": Book("yes", (Level(.49, 1000),), (Level(.50, 100),), time.time()),
            "no": Book("no", (Level(.49, 1000),), (Level(.50, 1000),), time.time()),
        })["m1"]
        spec = replace(build_agent_specs()[0], threshold=.002, min_liquidity=0)
        signal = Signal(spec.id, "m1", "Yes", .60, .05, 1, .0125,
                        "taker", "depth-sized", "Yes")
        decision = RiskManager().authorize(
            spec, signal, feature, AgentRiskState(10_000, 10_000, 10_000)
        )
        self.assertTrue(decision.allowed)
        self.assertAlmostEqual(decision.target_notional, 5.0)

    def test_maker_quote_only_fills_when_opposite_quote_crosses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = PaperBroker(Path(tmp) / "paper.sqlite3")
            spec = build_agent_specs()[90]
            broker.register_agents([spec])
            market = FeatureAndAgentTests().make_market()
            broker.db.execute(
                """INSERT INTO pending_orders(agent_id,market_id,event_id,token_id,outcome,side,
                   shares,limit_price,created_at,reason,strategy_version,decision_class)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (spec.id, "m1", "m1", "yes", "Yes", "BUY", 10, .49, time.time(),
                 "test quote", spec.strategy_version, "alpha"),
            )
            broker.db.commit()
            away = FeatureEngine().build([market], {
                "yes": Book("yes", (Level(.47, 1000),), (Level(.50, 1000),), time.time()),
                "no": Book("no", (Level(.50, 1000),), (Level(.53, 1000),), time.time()),
            })
            self.assertEqual(broker.process_pending(away), 0)
            crossed = FeatureEngine().build([market], {
                "yes": Book("yes", (Level(.48, 1000),), (Level(.49, 1000),), time.time()),
                "no": Book("no", (Level(.51, 1000),), (Level(.52, 1000),), time.time()),
            })
            self.assertEqual(broker.process_pending(crossed), 1)
            self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 1)
            broker.close()

    def test_negative_forward_evidence_pauses_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = PaperBroker(Path(tmp) / "paper.sqlite3")
            spec = build_agent_specs()[0]
            broker.register_agents([spec])
            market = FeatureAndAgentTests().make_market()
            feature = FeatureEngine().build([market], {
                "yes": Book("yes", (Level(.45, 1000),), (Level(.46, 1000),), time.time()),
                "no": Book("no", (Level(.54, 1000),), (Level(.55, 1000),), time.time()),
            })["m1"]
            now = time.time()
            broker.db.executemany(
                """INSERT INTO adaptation_evaluations(
                   agent_id,market_id,token_id,outcome,entry_price,entry_fee_per_share,
                   fee_rate,created_at,due_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [(spec.id, "m1", "yes", "Yes", .50, 0, 0, now - 600, now - 1,
                  spec.strategy_version) for _ in range(20)],
            )
            broker.db.commit()
            self.assertEqual(broker.resolve_adaptation({"m1": feature}), 20)
            state = broker.adaptation_states()[spec.id]
            self.assertEqual(state.state, "paused")
            self.assertEqual(state.allocation_multiplier, 0)
            self.assertLess(state.upper_bound or 0, 0)
            broker.close()

    def test_rejected_signal_creates_and_resolves_counterfactual_learning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = PaperBroker(Path(tmp) / "paper.sqlite3")
            spec = replace(build_agent_specs()[0], horizon=1)
            broker.register_agents([spec])
            market = replace(FeatureAndAgentTests().make_market(), fee_rate=0)
            feature = FeatureEngine().build([market], {
                "yes": Book("yes", (Level(.49, 1000),), (Level(.50, 1000),), time.time()),
                "no": Book("no", (Level(.49, 1000),), (Level(.50, 1000),), time.time()),
            })["m1"]
            signal = Signal(spec.id, "m1", None, .60, 0, 0, 0,
                            "taker", "below threshold", "Yes")
            self.assertTrue(broker.record_counterfactual(signal, feature, "below threshold"))
            self.assertFalse(broker.record_counterfactual(signal, feature, "duplicate"))
            broker.db.execute("UPDATE adaptation_evaluations SET due_at=?", (time.time() - 1,))
            broker.db.commit()
            future = FeatureEngine().build([market], {
                "yes": Book("yes", (Level(.55, 1000),), (Level(.56, 1000),), time.time()),
                "no": Book("no", (Level(.44, 1000),), (Level(.45, 1000),), time.time()),
            })["m1"]
            self.assertEqual(broker.resolve_adaptation({"m1": future}), 1)
            state = broker.adaptation_states()[spec.id]
            self.assertEqual(state.samples, 1)
            self.assertGreater(state.mean_return, 0)
            row = broker.db.execute(
                "SELECT evaluation_class,resolved_at FROM adaptation_evaluations"
            ).fetchone()
            self.assertEqual(row["evaluation_class"], "counterfactual-taker")
            self.assertIsNotNone(row["resolved_at"])
            broker.close()

    def test_feature_history_survives_restart_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.sqlite3"
            broker = PaperBroker(path)
            try:
                for index in range(140):
                    broker.append_feature_history([
                        ("m1", PricePoint(float(index), 0.4 + index / 1000, float(index)))
                    ])
                self.assertEqual(
                    broker.db.execute("SELECT COUNT(*) FROM market_history WHERE market_id='m1'").fetchone()[0],
                    128,
                )
                broker.close()
                broker = PaperBroker(path)
                features = FeatureEngine(initial_history=broker.load_feature_history())
                self.assertEqual(len(features.history["m1"]), 128)
                self.assertAlmostEqual(features.history["m1"][-1].mid, 0.539)
            finally:
                broker.close()

    def test_twelve_restarted_cycles_match_continuous_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            continuous_path = Path(tmp) / "continuous.sqlite3"
            restarted_path = Path(tmp) / "restarted.sqlite3"
            specs = build_agent_specs()

            continuous_broker = PaperBroker(continuous_path)
            try:
                continuous_engine = TradingEngine(
                    SyntheticMarketData(10), build_agents(specs), continuous_broker,
                )
                for _ in range(12):
                    continuous_engine.cycle(max_markets=10)
            finally:
                continuous_broker.close()

            for step in range(12):
                restarted_broker = PaperBroker(restarted_path)
                source = SyntheticMarketData(10)
                source.step = step
                try:
                    TradingEngine(
                        source, build_agents(specs), restarted_broker,
                    ).cycle(max_markets=10)
                finally:
                    restarted_broker.close()

            def signature(path: Path):
                import sqlite3
                db = sqlite3.connect(path)
                try:
                    trades = db.execute(
                        """SELECT agent_id,market_id,outcome,side,
                           ROUND(price,6),execution,decision_class FROM trades ORDER BY id"""
                    ).fetchall()
                    balances = db.execute(
                        "SELECT id,cash,equity FROM agents ORDER BY id"
                    ).fetchall()
                    return trades, balances
                finally:
                    db.close()

            continuous_trades, continuous_balances = signature(continuous_path)
            restarted_trades, restarted_balances = signature(restarted_path)
            self.assertEqual(continuous_trades, restarted_trades)
            self.assertEqual(
                [row[0] for row in continuous_balances],
                [row[0] for row in restarted_balances],
            )
            for expected, actual in zip(continuous_balances, restarted_balances, strict=True):
                self.assertAlmostEqual(expected[1], actual[1], places=2)
                self.assertAlmostEqual(expected[2], actual[2], places=2)

    def test_cycle_caps_new_positions_and_shadow_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = PaperBroker(Path(tmp) / "paper.sqlite3")
            try:
                engine = TradingEngine(SyntheticMarketData(20), build_agents(build_agent_specs()), broker)
                engine.cycle(max_markets=20)
                maximum = broker.db.execute(
                    "SELECT COALESCE(MAX(n),0) FROM (SELECT COUNT(DISTINCT market_id) n FROM positions GROUP BY agent_id)"
                ).fetchone()[0]
                statuses = dict(broker.db.execute(
                    "SELECT allocation_status,COUNT(*) FROM agents GROUP BY allocation_status"
                ).fetchall())
                self.assertLessEqual(maximum, 3)
                self.assertEqual(statuses, {"active": 90, "shadow": 10})
            finally:
                broker.close()

    def test_balanced_risk_throttles_and_kills_drawdown(self) -> None:
        feature = FeatureEngine().build(
            [FeatureAndAgentTests().make_market()],
            {
                "yes": Book("yes", (Level(.49, 1000),), (Level(.50, 1000),), 1),
                "no": Book("no", (Level(.49, 1000),), (Level(.50, 1000),), 1),
            },
        )["m1"]
        spec = build_agent_specs()[0]
        signal = Signal(spec.id, "m1", "Yes", .60, .08, 1.0, .0125, "taker", "test", "Yes")
        risk = RiskManager()
        healthy = risk.authorize(spec, signal, feature, AgentRiskState(10_000, 10_000, 10_000))
        throttled = risk.authorize(spec, signal, feature, AgentRiskState(9_000, 9_000, 10_000))
        killed = risk.authorize(spec, signal, feature, AgentRiskState(8_800, 8_800, 10_000))
        self.assertTrue(healthy.allowed)
        self.assertLessEqual(healthy.target_notional, 50.0)
        self.assertLess(throttled.target_notional, healthy.target_notional)
        self.assertFalse(killed.allowed)

    def test_hysteresis_exit_and_reentry_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = PaperBroker(Path(tmp) / "paper.sqlite3")
            spec = replace(
                build_agent_specs()[0], threshold=.01, risk_fraction=.01,
                min_liquidity=0, max_spread=1,
            )

            class ScriptedAgent:
                def __init__(self):
                    self.spec = spec
                    self.step = 0

                def decide(self, feature):
                    sequence = [
                        ("Yes", .02, "Yes"),
                        (None, .005, "Yes"),
                        (None, 0.0, "Yes"),
                        ("Yes", .015, "Yes"),
                        ("Yes", .025, "Yes"),
                    ]
                    outcome, edge, preferred = sequence[min(self.step, len(sequence) - 1)]
                    self.step += 1
                    return Signal(spec.id, feature.market.id, outcome, .55, edge, .5, .01,
                                  "taker", "scripted", preferred)

            try:
                engine = TradingEngine(
                    self.single_market_data(), [ScriptedAgent()], broker,
                )
                engine.cycle()
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 1)
                engine.cycle()
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
                engine.cycle()
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 2)
                engine.cycle()
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
                engine.cycle()
                self.assertEqual(broker.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 1)
            finally:
                broker.close()

    def test_public_client_has_no_order_submission_method(self) -> None:
        from polyalpha.api import PolymarketClient
        client = PolymarketClient()
        banned = {"order", "place_order", "submit_order", "trade"}
        self.assertTrue(banned.isdisjoint(set(dir(client))))


if __name__ == "__main__":
    unittest.main()
