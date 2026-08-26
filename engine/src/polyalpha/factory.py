from __future__ import annotations

from .models import AgentSpec


FAMILY_DESCRIPTIONS = {
    "momentum": "Multi-horizon trend continuation confirmed by order-book imbalance.",
    "mean_reversion": "Fade statistically extreme moves toward a rolling fair-value anchor.",
    "orderflow": "Use top-of-book depth imbalance and microprice pressure before price reacts.",
    "volatility_breakout": "Trade directional moves only when they are large relative to recent noise.",
    "crowd_bias": "Shrink overconfident longshots/favorites toward calibrated base probabilities.",
    "attention": "Require volume acceleration to confirm price information rather than noise.",
    "time_catalyst": "Change the trend/reversion mix as resolution approaches.",
    "relative_value": "Normalize mutually-exclusive negative-risk event outcomes and fade dislocations.",
    "complement_arb": "Buy complete YES+NO sets only when executable asks plus fees cost below $1.",
    "liquidity_maker": "Rest conservative fair-value orders to harvest spread without taker fees.",
}


def build_agent_specs() -> list[AgentSpec]:
    families = list(FAMILY_DESCRIPTIONS)
    specs: list[AgentSpec] = []
    for family_index, family in enumerate(families):
        for variant in range(1, 11):
            conservative = (variant - 1) / 9.0
            threshold = 0.006 + 0.0015 * variant
            max_spread = 0.035 + 0.004 * (variant % 4)
            min_liquidity = 1_000.0 + 750.0 * (variant % 5)
            execution = "maker" if family == "liquidity_maker" else "taker"
            specs.append(
                AgentSpec(
                    id=f"A{family_index * 10 + variant:03d}",
                    name=f"{family.replace('_', '-').title()}-{variant:02d}",
                    family=family,
                    variant=variant,
                    threshold=threshold if family != "complement_arb" else 0.001 + variant * 0.00025,
                    horizon=1 + (variant % 10),
                    risk_fraction=0.005 + 0.0075 * (1.0 - conservative),
                    max_spread=max_spread,
                    min_liquidity=min_liquidity,
                    execution=execution,
                    params={
                        "speed": 0.7 + 0.12 * variant,
                        "shrink": 0.08 + 0.025 * variant,
                        "imbalance": 0.4 + 0.1 * (variant % 5),
                        "vol_floor": 0.004 + 0.001 * (variant % 3),
                    },
                    allocation_status="shadow" if family == "crowd_bias" else "active",
                    allocation_tier="probation",
                    strategy_version="v2.1-continuous",
                )
            )
    assert len(specs) == 100
    assert len({spec.id for spec in specs}) == 100
    return specs
