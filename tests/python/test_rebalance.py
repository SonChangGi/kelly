from __future__ import annotations

from math import isclose

from kelly_lab.rebalance import simulate_rebalancing


def test_round_trip_path_has_positive_gross_rebalancing_effect() -> None:
    result = simulate_rebalancing(
        [[0.50, -1.0 / 3.0], [-1.0 / 3.0, 0.50]],
        [0.50, 0.50],
        dates=["2024-01-02", "2024-01-03"],
        frequency="daily",
        one_way_cost_bps=10,
    )

    assert result.gross_rebalancing_effect > 0
    assert result.trading_cost_drag > 0
    assert result.net_rebalancing_effect > 0
    assert result.total_turnover > 0
    assert result.net_weight_path[1] == [0.5, 0.5]


def test_trend_path_can_have_negative_rebalancing_effect() -> None:
    result = simulate_rebalancing(
        [[0.20, 0.0], [0.20, 0.0]],
        [0.50, 0.50],
        dates=["2024-01-02", "2024-01-03"],
        frequency="daily",
        one_way_cost_bps=0,
    )

    assert result.gross_rebalancing_effect < 0
    assert isclose(result.net_rebalancing_effect, result.gross_rebalancing_effect)


def test_leveraged_path_reports_ruin_without_clipping() -> None:
    result = simulate_rebalancing(
        [[-0.60]],
        [2.0],
        dates=["2024-01-02"],
        frequency="daily",
    )

    assert result.status == "ruin"
    assert result.reason == "ruin"
    assert result.net_rebalancing_effect is None


def test_none_frequency_has_no_turnover_or_cost() -> None:
    result = simulate_rebalancing(
        [[0.10, 0.0], [-0.05, 0.03]],
        [0.60, 0.40],
        dates=["2024-01-02", "2024-02-02"],
        frequency="none",
    )

    assert result.rebalance_count == 0
    assert result.total_turnover == 0
    assert result.trading_cost_paid == 0
    assert isclose(result.net_rebalancing_effect, 0.0, abs_tol=1e-15)


def test_effects_are_cagr_differences_not_terminal_wealth_differences() -> None:
    result = simulate_rebalancing(
        [[0.20, 0.0], [0.20, 0.0]],
        [0.50, 0.50],
        frequency="daily",
        one_way_cost_bps=0,
    )

    expected_buy_hold_cagr = result.buy_and_hold_wealth[-1] ** (252 / 2) - 1
    expected_gross_cagr = result.gross_rebalanced_wealth[-1] ** (252 / 2) - 1
    assert isclose(result.buy_and_hold_cagr, expected_buy_hold_cagr, rel_tol=1e-12)
    assert isclose(result.gross_rebalanced_cagr, expected_gross_cagr, rel_tol=1e-12)
    assert isclose(
        result.gross_rebalancing_effect,
        expected_gross_cagr - expected_buy_hold_cagr,
        rel_tol=1e-12,
    )


def test_fee_matches_actual_post_fee_target_notional() -> None:
    result = simulate_rebalancing(
        [[0.20, 0.0], [0.0, 0.0]],
        [0.50, 0.50],
        frequency="daily",
        one_way_cost_bps=10,
    )

    post_fee_nav = result.net_rebalanced_wealth[1]
    actual_traded_notional = abs(0.5 * post_fee_nav - 0.60) + abs(0.5 * post_fee_nav - 0.50)
    assert isclose(result.trading_cost_paid, 0.001 * actual_traded_notional, rel_tol=1e-10)
    assert result.net_weight_path[1] == [0.50, 0.50]


def test_final_observation_does_not_trigger_a_terminal_rebalance() -> None:
    result = simulate_rebalancing(
        [[0.0, 0.0], [0.20, 0.0]],
        [0.50, 0.50],
        dates=["2024-01-02", "2024-01-03"],
        frequency="daily",
        one_way_cost_bps=10,
    )

    assert result.trading_cost_paid == 0.0
    assert result.total_turnover == 0.0
    assert result.net_weight_path[-1][0] > 0.50


def test_n_return_dates_use_observation_count_for_cagr() -> None:
    result = simulate_rebalancing(
        [[0.20, 0.0], [0.0, 0.0]],
        [0.50, 0.50],
        dates=["2024-01-02", "2024-01-03"],
        frequency="none",
        one_way_cost_bps=0,
    )

    expected = result.buy_and_hold_wealth[-1] ** (252 / 2) - 1
    assert isclose(result.buy_and_hold_cagr, expected, rel_tol=1e-12)


def test_n_plus_one_dates_use_full_calendar_span_for_rebalancing_cagr() -> None:
    result = simulate_rebalancing(
        [[0.10]],
        [1.0],
        dates=["2023-01-01", "2024-01-01"],
        frequency="none",
    )

    expected = 1.10 ** (365.2425 / 365) - 1
    assert isclose(result.buy_and_hold_cagr, expected, rel_tol=1e-12)
