"""Path simulation for frequency, financing, turnover, and trading-cost drag."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from math import expm1, isfinite, log

from .errors import KellyLabError, ReasonCode
from .metrics import TRADING_DAYS_PER_YEAR, annual_rate_to_periodic

REBALANCE_FREQUENCIES = ("none", "daily", "weekly", "monthly", "quarterly", "yearly")


@dataclass(frozen=True)
class RebalanceResult:
    dates: list[str]
    buy_and_hold_wealth: list[float]
    gross_rebalanced_wealth: list[float]
    net_rebalanced_wealth: list[float]
    net_weight_path: list[list[float] | None]
    buy_and_hold_cagr: float | None
    gross_rebalanced_cagr: float | None
    net_rebalanced_cagr: float | None
    gross_rebalancing_effect: float | None
    trading_cost_drag: float | None
    net_rebalancing_effect: float | None
    total_turnover: float
    trading_cost_paid: float
    rebalance_count: int
    frequency: str
    status: str = "published"
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class _PathState:
    asset_values: list[float]
    cash_value: float
    wealth: list[float]
    weights: list[list[float] | None]
    turnover: float = 0.0
    cost_paid: float = 0.0
    rebalance_count: int = 0
    ruined: bool = False


def _date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise KellyLabError(ReasonCode.INVALID_DATES, "dates must be ISO-8601 dates") from error


def _period_key(value: date, frequency: str) -> tuple[int, ...]:
    if frequency == "weekly":
        iso_year, week, _ = value.isocalendar()
        return (iso_year, week)
    if frequency == "monthly":
        return (value.year, value.month)
    if frequency == "quarterly":
        return (value.year, (value.month - 1) // 3 + 1)
    if frequency == "yearly":
        return (value.year,)
    return ()


def _rebalance_after(index: int, dates: Sequence[date], frequency: str) -> bool:
    if index >= len(dates) - 1 or frequency == "none":
        return False
    if frequency == "daily":
        return True
    return _period_key(dates[index], frequency) != _period_key(dates[index + 1], frequency)


def _current_weights(state: _PathState, nav: float) -> list[float] | None:
    if nav <= 0:
        return None
    return [value / nav for value in state.asset_values]


def _advance(
    state: _PathState,
    asset_returns: Sequence[float],
    *,
    risk_free_periodic: float,
    borrowing_periodic: float,
) -> float | None:
    for index, asset_return in enumerate(asset_returns):
        multiplier = 1.0 + asset_return
        if multiplier < 0:
            state.ruined = True
            state.wealth.append(float("nan"))
            state.weights.append(None)
            return None
        state.asset_values[index] *= multiplier
    cash_multiplier = (
        1.0 + risk_free_periodic if state.cash_value >= 0 else 1.0 + borrowing_periodic
    )
    state.cash_value *= cash_multiplier
    nav = sum(state.asset_values) + state.cash_value
    if nav <= 0 or not isfinite(nav):
        state.ruined = True
        state.wealth.append(float("nan"))
        state.weights.append(None)
        return None
    state.wealth.append(nav)
    state.weights.append(_current_weights(state, nav))
    return nav


def _rebalance(
    state: _PathState,
    target_weights: Sequence[float],
    nav: float,
    transaction_cost_rate: float,
) -> None:
    if transaction_cost_rate == 0:
        after_cost = nav
        traded_notional = sum(
            abs(nav * target - current)
            for target, current in zip(target_weights, state.asset_values, strict=True)
        )
    else:
        # Solve V_after = V_before - c * sum(|target_i * V_after - holding_i|).
        # This makes the reported fee equal the actual traded notional while
        # preserving exact target weights after the fee is paid.
        def balance(after_fee_nav: float) -> float:
            traded = sum(
                abs(after_fee_nav * target - current)
                for target, current in zip(target_weights, state.asset_values, strict=True)
            )
            return after_fee_nav + transaction_cost_rate * traded - nav

        lower = 0.0
        upper = nav
        if balance(lower) > 0:
            state.ruined = True
            state.wealth[-1] = float("nan")
            state.weights[-1] = None
            return
        for _ in range(100):
            middle = (lower + upper) / 2.0
            if balance(middle) > 0:
                upper = middle
            else:
                lower = middle
        after_cost = (lower + upper) / 2.0
        traded_notional = sum(
            abs(after_cost * target - current)
            for target, current in zip(target_weights, state.asset_values, strict=True)
        )
    cost = nav - after_cost
    if after_cost <= 0:
        state.ruined = True
        state.wealth[-1] = float("nan")
        state.weights[-1] = None
        return
    state.turnover += traded_notional / nav
    state.cost_paid += cost
    state.rebalance_count += 1
    state.asset_values = [after_cost * weight for weight in target_weights]
    state.cash_value = after_cost * (1.0 - sum(target_weights))
    state.wealth[-1] = after_cost
    state.weights[-1] = list(target_weights)


def _annualized_return(
    final_wealth: float,
    observations: int,
    *,
    date_span: tuple[date, date] | None,
    annualization: int,
) -> float | None:
    if final_wealth <= 0 or observations <= 0:
        return None
    years: float
    if date_span is not None and date_span[1] > date_span[0]:
        years = (date_span[1] - date_span[0]).days / 365.2425
    else:
        years = observations / annualization
    if years <= 0:
        return None
    return expm1(log(final_wealth) / years)


def _initial_state(target_weights: Sequence[float]) -> _PathState:
    return _PathState(
        asset_values=[float(weight) for weight in target_weights],
        cash_value=1.0 - sum(target_weights),
        wealth=[1.0],
        weights=[list(target_weights)],
    )


def simulate_rebalancing(
    returns_matrix: Sequence[Sequence[float]],
    target_weights: Sequence[float],
    *,
    dates: Sequence[date | datetime | str] | None = None,
    frequency: str = "monthly",
    one_way_cost_bps: float = 10.0,
    risk_free_rate: float = 0.0,
    borrowing_spread: float = 0.0,
    annualization: int = TRADING_DAYS_PER_YEAR,
) -> RebalanceResult:
    """Compare no rebalance, gross rebalance, and net rebalance paths.

    Turnover is traded asset notional divided by pre-trade NAV.  A 10 bp
    one-way cost is charged on every buy and sell dollar represented in that
    notional.  Initial allocation is not counted as turnover.
    """

    if frequency not in REBALANCE_FREQUENCIES:
        raise KellyLabError(
            ReasonCode.INVALID_FREQUENCY,
            f"frequency must be one of {', '.join(REBALANCE_FREQUENCIES)}",
        )
    weights = [float(weight) for weight in target_weights]
    if not weights or any(not isfinite(weight) or weight < 0 for weight in weights):
        raise KellyLabError(
            ReasonCode.INVALID_TARGET_WEIGHTS,
            "target weights must be a non-empty, finite, long-only vector",
        )
    if sum(weights) > 3.0 + 1e-12:
        raise KellyLabError(
            ReasonCode.INVALID_TARGET_WEIGHTS,
            "target exposure cannot exceed the v1 3x cap",
        )
    cost_bps = float(one_way_cost_bps)
    if not isfinite(cost_bps) or cost_bps < 0:
        raise KellyLabError(ReasonCode.INVALID_COST, "cost bps must be finite and non-negative")
    if not isfinite(risk_free_rate) or not isfinite(borrowing_spread) or borrowing_spread < 0:
        raise KellyLabError(ReasonCode.INVALID_RATE, "rates must be finite and spread non-negative")

    rows = [[float(value) for value in row] for row in returns_matrix]
    if any(len(row) != len(weights) for row in rows):
        raise KellyLabError(
            ReasonCode.INVALID_TARGET_WEIGHTS,
            "every return row must match target weight dimensions",
        )
    if any(not isfinite(value) for row in rows for value in row):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "returns must be finite")

    if not rows:
        raise KellyLabError(
            ReasonCode.INSUFFICIENT_OBSERVATIONS,
            "at least one return row is required",
        )

    date_span: tuple[date, date] | None = None
    if dates is None:
        parsed_dates = [date(2000, 1, 1) + timedelta(days=index) for index in range(len(rows))]
    else:
        if len(dates) not in (len(rows), len(rows) + 1):
            raise KellyLabError(
                ReasonCode.INVALID_DATES,
                "dates must contain N return dates or the preferred N+1 price dates",
            )
        all_dates = [_date(value) for value in dates]
        if any(right <= left for left, right in zip(all_dates, all_dates[1:], strict=False)):
            raise KellyLabError(ReasonCode.INVALID_DATES, "dates must be strictly increasing")
        date_span = (all_dates[0], all_dates[-1])
        parsed_dates = all_dates[1:] if len(all_dates) == len(rows) + 1 else all_dates

    risk_free_periodic = annual_rate_to_periodic(risk_free_rate, annualization)
    if risk_free_rate + borrowing_spread <= -1:
        raise KellyLabError(ReasonCode.INVALID_RATE, "borrowing rate must be greater than -1")
    borrowing_periodic = annual_rate_to_periodic(risk_free_rate + borrowing_spread, annualization)
    transaction_cost_rate = cost_bps / 10_000.0

    buy_hold = _initial_state(weights)
    gross = _initial_state(weights)
    net = _initial_state(weights)
    for index, row in enumerate(rows):
        buy_hold_nav = _advance(
            buy_hold,
            row,
            risk_free_periodic=risk_free_periodic,
            borrowing_periodic=borrowing_periodic,
        )
        gross_nav = _advance(
            gross,
            row,
            risk_free_periodic=risk_free_periodic,
            borrowing_periodic=borrowing_periodic,
        )
        net_nav = _advance(
            net,
            row,
            risk_free_periodic=risk_free_periodic,
            borrowing_periodic=borrowing_periodic,
        )
        if buy_hold_nav is None or gross_nav is None or net_nav is None:
            break
        if _rebalance_after(index, parsed_dates, frequency):
            _rebalance(gross, weights, gross_nav, 0.0)
            _rebalance(net, weights, net_nav, transaction_cost_rate)
            if gross.ruined or net.ruined:
                break

    path_dates = [value.isoformat() for value in parsed_dates[: len(net.wealth) - 1]]
    output_dates = (["initial"] + path_dates)[: len(net.wealth)]
    ruined = buy_hold.ruined or gross.ruined or net.ruined
    if ruined:
        return RebalanceResult(
            dates=output_dates,
            buy_and_hold_wealth=buy_hold.wealth,
            gross_rebalanced_wealth=gross.wealth,
            net_rebalanced_wealth=net.wealth,
            net_weight_path=net.weights,
            buy_and_hold_cagr=None,
            gross_rebalanced_cagr=None,
            net_rebalanced_cagr=None,
            gross_rebalancing_effect=None,
            trading_cost_drag=None,
            net_rebalancing_effect=None,
            total_turnover=net.turnover,
            trading_cost_paid=net.cost_paid,
            rebalance_count=net.rebalance_count,
            frequency=frequency,
            status="ruin",
            reason=ReasonCode.RUIN.value,
        )

    buy_hold_cagr = _annualized_return(
        buy_hold.wealth[-1], len(rows), date_span=date_span, annualization=annualization
    )
    gross_cagr = _annualized_return(
        gross.wealth[-1], len(rows), date_span=date_span, annualization=annualization
    )
    net_cagr = _annualized_return(
        net.wealth[-1], len(rows), date_span=date_span, annualization=annualization
    )
    gross_effect = (
        gross_cagr - buy_hold_cagr if gross_cagr is not None and buy_hold_cagr is not None else None
    )
    cost_drag = gross_cagr - net_cagr if gross_cagr is not None and net_cagr is not None else None
    net_effect = (
        net_cagr - buy_hold_cagr if net_cagr is not None and buy_hold_cagr is not None else None
    )
    return RebalanceResult(
        dates=output_dates,
        buy_and_hold_wealth=buy_hold.wealth,
        gross_rebalanced_wealth=gross.wealth,
        net_rebalanced_wealth=net.wealth,
        net_weight_path=net.weights,
        buy_and_hold_cagr=buy_hold_cagr,
        gross_rebalanced_cagr=gross_cagr,
        net_rebalanced_cagr=net_cagr,
        gross_rebalancing_effect=gross_effect,
        trading_cost_drag=cost_drag,
        net_rebalancing_effect=net_effect,
        total_turnover=net.turnover,
        trading_cost_paid=net.cost_paid,
        rebalance_count=net.rebalance_count,
        frequency=frequency,
    )
