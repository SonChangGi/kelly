from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Any

from .errors import KellyLabError, ReasonCode
from .fx import align_fx_prior, convert_prices_to_base, simple_returns_from_prices
from .kelly import exact_historical_kelly, single_asset_gbm_kelly
from .metrics import calculate_metrics
from .portfolio import (
    MIN_COMMON_OBSERVATIONS,
    covariance_from_correlation,
    estimate_covariance,
    multi_asset_exact_kelly,
    multi_asset_gbm_kelly,
)
from .rebalance import REBALANCE_FREQUENCIES, simulate_rebalancing

USABLE_DATA_STATES = {"published", "live_api", "stale", "degraded"}
MIN_HISTORICAL_OBSERVATIONS = 60


class CLIError(ValueError):
    """A CLI contract error with a stable, machine-readable reason."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _rate(value: float) -> float:
    return float(value)


def _read_object(path_value: str) -> tuple[Path, dict[str, Any]]:
    path = Path(path_value)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CLIError("invalid_input", f"{path} must contain a JSON object")
    return path, payload


def _read_available_asset(path_value: str) -> tuple[Path, dict[str, Any]]:
    path, payload = _read_object(path_value)
    if payload.get("state") not in USABLE_DATA_STATES:
        raise CLIError("data_unavailable", f"{path} is not in a usable data state")
    return path, payload


def _series_currency(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise CLIError("missing_field", "asset metadata is required")
    # An FX quote is already measured in its quote currency.  Treating
    # USD/KRW as an ordinary USD asset would multiply it by itself during a
    # KRW conversion and square the exchange-rate return.
    currency_field = "quoteCurrency" if metadata.get("assetType") == "fx" else "baseCurrency"
    if not metadata.get(currency_field):
        raise CLIError("missing_field", f"asset metadata.{currency_field} is required")
    currency = str(metadata[currency_field]).upper()
    if currency not in {"KRW", "USD"}:
        raise CLIError("unsupported_currency", f"unsupported series currency: {currency}")
    return currency


def _fx_series(payload: dict[str, Any]) -> tuple[list[str], list[float]]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or any(
        (
            metadata.get("assetType") != "fx",
            metadata.get("symbol") != "USD/KRW",
            metadata.get("baseCurrency") != "USD",
            metadata.get("quoteCurrency") != "KRW",
            metadata.get("returnBasis") != "fx_rate",
        )
    ):
        raise KellyLabError(
            ReasonCode.FX_MISSING,
            "--fx must be the normalized USD/KRW FX asset contract",
        )
    dates = list(payload["dates"])
    rates = payload.get("prices")
    if not isinstance(rates, list):
        raise KellyLabError(ReasonCode.FX_MISSING, "FX asset must contain prices or fx rates")
    return dates, [float(value) for value in rates]


def _convert_usd_prices_to_krw(
    dates: list[str],
    prices: list[float],
    fx_payload: dict[str, Any],
) -> tuple[list[float], dict[str, object]]:
    fx_dates, fx_rates = _fx_series(fx_payload)
    alignment = align_fx_prior(dates, fx_dates, fx_rates, max_lag_days=5)
    if alignment.status != "published":
        raise KellyLabError(
            alignment.reason or ReasonCode.FX_MISSING,
            "every selected USD price date requires a prior FX fix no more than five days old",
        )
    detail = alignment.as_dict()
    detail.update(
        {
            "baseCurrency": "USD",
            "quoteCurrency": "KRW",
            "conversionApplied": True,
        }
    )
    return convert_prices_to_base(prices, alignment.rates), detail


def _fx_alignment_summary(detail: dict[str, object]) -> dict[str, object]:
    lags = [value for value in detail.get("lag_days", []) if isinstance(value, int)]
    source_dates = [value for value in detail.get("source_dates", []) if isinstance(value, str)]
    return {
        "status": detail.get("status"),
        "reason": detail.get("reason"),
        "baseCurrency": "USD",
        "quoteCurrency": "KRW",
        "conversionApplied": True,
        "maxLagDays": detail.get("max_lag_days", 5),
        "maxObservedLagDays": max(lags, default=None),
        "alignedPriceCount": len(detail.get("rates", [])),
        "sourceDateFrom": source_dates[0] if source_dates else None,
        "sourceDateTo": source_dates[-1] if source_dates else None,
    }


def assumptions(args: argparse.Namespace) -> int:
    result = single_asset_gbm_kelly(
        _rate(args.excess_return),
        _rate(args.volatility),
        risk_free_rate=_rate(args.risk_free),
        borrowing_spread=_rate(args.borrowing_spread),
        leverage_cap=_rate(args.cap),
    )
    _json(
        {
            "contract": "kelly-cli-result",
            "mode": "direct_assumptions",
            "status": result.status,
            "reason": result.reason,
            "inputs": {
                "expectedExcessReturn": args.excess_return,
                "volatility": args.volatility,
                "riskFreeRate": args.risk_free,
                "borrowingSpread": args.borrowing_spread,
                "leverageCap": args.cap,
            },
            "result": result.as_dict(),
            "disclaimer": "Assumption sensitivity, not an investment recommendation.",
        }
    )
    return 0 if result.status in {"published", "degraded"} else 2


def _slice_prices(
    dates: list[str], prices: list[float], start: str | None, end: str | None
) -> tuple[list[str], list[float]]:
    if len(dates) != len(prices):
        raise CLIError("invalid_input", "asset dates and prices must have the same length")
    if any(not isfinite(float(price)) or float(price) <= 0 for price in prices):
        raise KellyLabError(
            ReasonCode.INVALID_RETURN,
            "asset prices must be positive and finite",
        )
    try:
        parsed_dates = [date.fromisoformat(value) for value in dates]
        parsed_start = date.fromisoformat(start) if start is not None else None
        parsed_end = date.fromisoformat(end) if end is not None else None
    except (TypeError, ValueError) as error:
        raise KellyLabError(ReasonCode.INVALID_DATES, "dates must be ISO-8601 dates") from error
    if any(right <= left for left, right in zip(parsed_dates, parsed_dates[1:], strict=False)):
        raise KellyLabError(ReasonCode.INVALID_DATES, "asset dates must be strictly increasing")
    if parsed_start is not None and parsed_end is not None and parsed_start > parsed_end:
        raise KellyLabError(ReasonCode.INVALID_DATES, "start date cannot be after end date")
    selected = [
        (day, float(price))
        for day, price in zip(dates, prices, strict=True)
        if (start is None or day >= start) and (end is None or day <= end)
    ]
    if len(selected) < 2:
        raise KellyLabError(
            ReasonCode.INSUFFICIENT_OBSERVATIONS,
            "the selected period must contain at least two prices",
        )
    return [row[0] for row in selected], [row[1] for row in selected]


def analyze(args: argparse.Namespace) -> int:
    _, payload = _read_available_asset(args.asset)
    dates, prices = _slice_prices(
        list(payload["dates"]),
        [float(value) for value in payload["prices"]],
        args.start,
        args.end,
    )
    base_currency = _series_currency(payload)
    fx_alignment: dict[str, object] | None = None
    if args.currency == "krw":
        if base_currency == "KRW":
            fx_alignment = {
                "status": "published",
                "reason": None,
                "baseCurrency": "KRW",
                "quoteCurrency": "KRW",
                "conversionApplied": False,
                "maxLagDays": 5,
            }
        else:
            if not args.fx:
                raise KellyLabError(
                    ReasonCode.FX_MISSING,
                    "--fx is required to convert a USD asset to KRW",
                )
            _, fx_payload = _read_available_asset(args.fx)
            prices, fx_alignment = _convert_usd_prices_to_krw(dates, prices, fx_payload)

    returns = simple_returns_from_prices(prices)
    if len(returns) < MIN_HISTORICAL_OBSERVATIONS:
        raise KellyLabError(
            ReasonCode.INSUFFICIENT_OBSERVATIONS,
            f"historical analysis requires at least {MIN_HISTORICAL_OBSERVATIONS} returns",
        )
    sortino_mar = args.risk_free if args.mar is None else args.mar
    metrics = calculate_metrics(
        returns,
        dates=dates,
        risk_free_rate=args.risk_free,
        mar=args.mar,
    )
    if metrics.annual_arithmetic_return is None or metrics.annual_volatility is None:
        raise ValueError("HISTORICAL_KELLY_INPUT_UNAVAILABLE")
    expected_excess = metrics.annual_arithmetic_return - args.risk_free
    gbm = single_asset_gbm_kelly(
        expected_excess,
        metrics.annual_volatility,
        risk_free_rate=args.risk_free,
        borrowing_spread=args.borrowing_spread,
        leverage_cap=args.cap,
    )
    exact = exact_historical_kelly(
        returns,
        risk_free_rate=args.risk_free,
        borrowing_spread=args.borrowing_spread,
        leverage_cap=args.cap,
    )
    synthetic_two_x = simulate_rebalancing(
        [[value] for value in returns],
        [2.0],
        dates=dates,
        frequency="daily",
        one_way_cost_bps=0.0,
        risk_free_rate=args.risk_free,
        borrowing_spread=args.borrowing_spread,
    )
    calculation_statuses = {metrics.status, gbm.status, exact.status, synthetic_two_x.status}
    if "ruin" in calculation_statuses:
        analysis_status = "ruin"
    elif calculation_statuses == {"published"}:
        analysis_status = "published"
    else:
        analysis_status = "degraded"
    _json(
        {
            "contract": "kelly-cli-result",
            "mode": "historical",
            "status": analysis_status,
            "assetId": payload.get("assetId"),
            "state": payload.get("state"),
            "period": {"start": dates[0], "end": dates[-1]},
            "inputs": {
                "currency": args.currency,
                "riskFreeRate": args.risk_free,
                "sortinoMar": sortino_mar,
                "sortinoMarSource": "risk_free_rate" if args.mar is None else "explicit",
                "borrowingSpread": args.borrowing_spread,
                "leverageCap": args.cap,
            },
            "returnBasis": (payload.get("metadata") or {}).get("returnBasis"),
            "fxAlignment": fx_alignment,
            "metrics": metrics.as_dict(),
            "gbmKelly": gbm.as_dict(),
            "exactInSampleKelly": exact.as_dict(),
            "syntheticDailyTarget2x": synthetic_two_x.as_dict(),
            "disclaimer": "Selected-period in-sample research, not a forecast.",
        }
    )
    return 0


def portfolio(args: argparse.Namespace) -> int:
    expected = [float(value) for value in args.excess_returns.split(",")]
    volatility = [float(value) for value in args.volatilities.split(",")]
    correlation = json.loads(Path(args.correlation).read_text(encoding="utf-8"))
    if len(expected) != len(volatility):
        raise ValueError("expected returns and volatilities must have the same length")
    covariance = covariance_from_correlation(volatility, correlation)
    result = multi_asset_gbm_kelly(
        expected,
        covariance,
        risk_free_rate=args.risk_free,
        borrowing_spread=args.borrowing_spread,
        leverage_cap=args.cap,
    )
    _json(
        {
            "contract": "kelly-cli-result",
            "mode": "multi_asset_assumptions",
            "status": result.status,
            "reason": result.reason,
            "inputs": {
                "expectedExcessReturns": expected,
                "volatilities": volatility,
                "correlation": correlation,
                "riskFreeRate": args.risk_free,
                "borrowingSpread": args.borrowing_spread,
                "leverageCap": args.cap,
            },
            "covariance": covariance.tolist(),
            "result": result.as_dict(),
            "disclaimer": "Input-driven model diagnostic, not an investment recommendation.",
        }
    )
    return 0 if result.status in {"published", "degraded"} else 2


def _return_series(
    payload: dict[str, Any],
    start: str | None,
    end: str | None,
    fx_payload: dict[str, Any] | None,
) -> tuple[list[str], list[str], dict[str, float], dict[str, object]]:
    dates, prices = _slice_prices(
        list(payload["dates"]),
        [float(value) for value in payload["prices"]],
        start,
        end,
    )
    base_currency = _series_currency(payload)
    if base_currency == "USD":
        if fx_payload is None:
            raise KellyLabError(
                ReasonCode.FX_MISSING,
                "--fx is required when portfolio-history contains a USD asset",
            )
        prices, fx_detail = _convert_usd_prices_to_krw(dates, prices, fx_payload)
        fx_summary = _fx_alignment_summary(fx_detail)
    else:
        fx_summary = {
            "status": "published",
            "reason": None,
            "baseCurrency": "KRW",
            "quoteCurrency": "KRW",
            "conversionApplied": False,
            "maxLagDays": 5,
        }
    returns = simple_returns_from_prices(prices)
    return dates, dates[1:], dict(zip(dates[1:], returns, strict=True)), fx_summary


def portfolio_history(args: argparse.Namespace) -> int:
    if len(args.assets) < 2:
        raise CLIError("invalid_input", "portfolio-history requires at least two asset JSON files")
    if args.annualization <= 0:
        raise CLIError("invalid_input", "annualization must be positive")

    payloads: list[dict[str, Any]] = []
    paths: list[Path] = []
    base_currencies: list[str] = []
    for asset_path in args.assets:
        path, payload = _read_available_asset(asset_path)
        paths.append(path)
        payloads.append(payload)
        base_currencies.append(_series_currency(payload))

    fx_path: Path | None = None
    fx_payload: dict[str, Any] | None = None
    if "USD" in base_currencies:
        if not args.fx:
            raise KellyLabError(
                ReasonCode.FX_MISSING,
                "--fx is required when portfolio-history contains a USD asset",
            )
        fx_path, fx_payload = _read_available_asset(args.fx)

    return_maps: list[dict[str, float]] = []
    fx_summaries: list[dict[str, object]] = []
    for payload in payloads:
        _, _, return_map, fx_summary = _return_series(payload, args.start, args.end, fx_payload)
        return_maps.append(return_map)
        fx_summaries.append(fx_summary)

    common_dates = sorted(set.intersection(*(set(values) for values in return_maps)))
    observations = len(common_dates)
    if observations < MIN_COMMON_OBSERVATIONS:
        _json(
            {
                "contract": "kelly-cli-result",
                "mode": "portfolio_history",
                "status": "unavailable",
                "reason": ReasonCode.INSUFFICIENT_COMMON_OBSERVATIONS.value,
                "commonObservations": observations,
                "minimumCommonObservations": MIN_COMMON_OBSERVATIONS,
            }
        )
        return 2

    returns_matrix = [[return_map[day] for return_map in return_maps] for day in common_dates]
    covariance = estimate_covariance(
        returns_matrix,
        annualization=args.annualization,
        minimum_common_observations=MIN_COMMON_OBSERVATIONS,
    )
    if covariance.covariance is None or covariance.correlation is None:
        raise KellyLabError(
            covariance.reason or ReasonCode.INSUFFICIENT_COMMON_OBSERVATIONS,
            "historical covariance is unavailable",
        )
    annual_arithmetic = [
        sum(row[index] for row in returns_matrix) / observations * args.annualization
        for index in range(len(return_maps))
    ]
    expected_excess = [value - args.risk_free for value in annual_arithmetic]
    gbm = multi_asset_gbm_kelly(
        expected_excess,
        covariance.covariance,
        risk_free_rate=args.risk_free,
        borrowing_spread=args.borrowing_spread,
        leverage_cap=args.cap,
        common_observations=observations,
        minimum_common_observations=MIN_COMMON_OBSERVATIONS,
    )
    exact = multi_asset_exact_kelly(
        returns_matrix,
        risk_free_rate=args.risk_free,
        borrowing_spread=args.borrowing_spread,
        annualization=args.annualization,
        leverage_cap=args.cap,
        minimum_common_observations=MIN_COMMON_OBSERVATIONS,
    )
    result_statuses = {gbm.status, exact.status}
    status = "published" if result_statuses == {"published"} else "degraded"
    _json(
        {
            "contract": "kelly-cli-result",
            "mode": "portfolio_history",
            "status": status,
            "currency": "KRW",
            "assets": [
                {
                    "assetId": payload.get("assetId", path.stem),
                    "path": str(path),
                    "returnBasis": (payload.get("metadata") or {}).get("returnBasis"),
                }
                for path, payload in zip(paths, payloads, strict=True)
            ],
            "fxAlignment": {
                "required": "USD" in base_currencies,
                "source": str(fx_path) if fx_path is not None else None,
                "maxLagDays": 5,
                "assets": [
                    {"assetId": payload.get("assetId", path.stem), **summary}
                    for path, payload, summary in zip(paths, payloads, fx_summaries, strict=True)
                ],
            },
            "period": {"start": common_dates[0], "end": common_dates[-1]},
            "commonDates": common_dates,
            "commonObservations": observations,
            "historicalEstimates": {
                "annualization": args.annualization,
                "annualArithmeticReturns": annual_arithmetic,
                "expectedExcessReturns": expected_excess,
                "covariance": covariance.covariance,
                "correlation": covariance.correlation,
            },
            "gbmKelly": gbm.as_dict(),
            "exactInSampleKelly": exact.as_dict(),
            "disclaimer": "Common-period in-sample research, not a forecast.",
        }
    )
    return 0


def rebalance(args: argparse.Namespace) -> int:
    if args.annualization <= 0:
        raise CLIError("invalid_input", "annualization must be positive")
    _, payload = _read_object(args.input)
    result = simulate_rebalancing(
        payload["returnsMatrix"],
        payload["targetWeights"],
        dates=payload["dates"],
        frequency=args.frequency,
        one_way_cost_bps=args.cost_bps,
        risk_free_rate=args.risk_free,
        borrowing_spread=args.borrowing_spread,
        annualization=args.annualization,
    )
    _json(
        {
            "contract": "kelly-cli-result",
            "mode": "rebalance",
            "status": result.status,
            "inputs": {
                "frequency": args.frequency,
                "oneWayCostBps": args.cost_bps,
                "riskFreeRate": args.risk_free,
                "borrowingSpread": args.borrowing_spread,
                "annualization": args.annualization,
            },
            "result": result.as_dict(),
            "disclaimer": "Historical path diagnostic, not an investment recommendation.",
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kelly-lab", description="Kelly Allocation Lab CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    direct = subparsers.add_parser("assumptions", help="calculate a single-asset GBM Kelly case")
    direct.add_argument(
        "--excess-return", type=float, required=True, help="annual decimal, e.g. 0.06"
    )
    direct.add_argument("--volatility", type=float, required=True, help="annual decimal, e.g. 0.20")
    direct.add_argument("--risk-free", type=float, default=0.0)
    direct.add_argument("--borrowing-spread", type=float, default=0.0)
    direct.add_argument("--cap", type=float, default=3.0)
    direct.set_defaults(handler=assumptions)

    history = subparsers.add_parser("analyze", help="analyze a normalized static asset file")
    history.add_argument("asset")
    history.add_argument("--start")
    history.add_argument("--end")
    history.add_argument("--currency", choices=("native", "krw"), default="native")
    history.add_argument("--fx", help="normalized USD/KRW asset JSON used for KRW conversion")
    history.add_argument("--risk-free", type=float, default=0.0)
    history.add_argument("--mar", type=float)
    history.add_argument("--borrowing-spread", type=float, default=0.0)
    history.add_argument("--cap", type=float, default=3.0)
    history.set_defaults(handler=analyze)

    multi = subparsers.add_parser("portfolio", help="calculate a multi-asset GBM allocation")
    multi.add_argument("--excess-returns", required=True, help="comma-separated annual decimals")
    multi.add_argument("--volatilities", required=True, help="comma-separated annual decimals")
    multi.add_argument("--correlation", required=True, help="path to a JSON matrix")
    multi.add_argument("--risk-free", type=float, default=0.0)
    multi.add_argument("--borrowing-spread", type=float, default=0.0)
    multi.add_argument("--cap", type=float, default=3.0)
    multi.set_defaults(handler=portfolio)

    historical_multi = subparsers.add_parser(
        "portfolio-history", help="estimate and optimize a common-date historical portfolio"
    )
    historical_multi.add_argument("assets", nargs="+")
    historical_multi.add_argument("--start")
    historical_multi.add_argument("--end")
    historical_multi.add_argument(
        "--fx", help="normalized USD/KRW asset JSON required for each USD asset"
    )
    historical_multi.add_argument("--risk-free", type=float, default=0.0)
    historical_multi.add_argument("--borrowing-spread", type=float, default=0.0)
    historical_multi.add_argument("--cap", type=float, default=3.0)
    historical_multi.add_argument("--annualization", type=int, default=252)
    historical_multi.set_defaults(handler=portfolio_history)

    rebalance_path = subparsers.add_parser(
        "rebalance", help="simulate rebalancing from a path input JSON"
    )
    rebalance_path.add_argument("input")
    rebalance_path.add_argument(
        "--frequency", default="monthly", metavar="{" + ",".join(REBALANCE_FREQUENCIES) + "}"
    )
    rebalance_path.add_argument("--cost-bps", "--cost", dest="cost_bps", type=float, default=10.0)
    rebalance_path.add_argument("--risk-free", type=float, default=0.0)
    rebalance_path.add_argument("--borrowing-spread", type=float, default=0.0)
    rebalance_path.add_argument("--annualization", type=int, default=252)
    rebalance_path.set_defaults(handler=rebalance)
    return parser


def _error_reason(error: Exception) -> str:
    if isinstance(error, KellyLabError):
        return error.code.value
    if isinstance(error, CLIError):
        return error.reason
    if isinstance(error, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(error, OSError):
        return "input_unavailable"
    if isinstance(error, KeyError):
        return "missing_field"
    return "invalid_input"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (KellyLabError, ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as error:
        mode = {
            "assumptions": "direct_assumptions",
            "analyze": "historical",
            "portfolio": "multi_asset_assumptions",
            "portfolio-history": "portfolio_history",
            "rebalance": "rebalance",
        }.get(args.command, args.command.replace("-", "_"))
        _json(
            {
                "contract": "kelly-cli-result",
                "mode": mode,
                "status": "unavailable",
                "reason": _error_reason(error),
                "message": str(error),
            }
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
