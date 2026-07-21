from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import KellyLabError
from .fx import simple_returns_from_prices
from .kelly import exact_historical_kelly, single_asset_gbm_kelly
from .metrics import calculate_metrics
from .portfolio import covariance_from_correlation, multi_asset_gbm_kelly


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _rate(value: float) -> float:
    return float(value)


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
    return 0


def _slice_prices(
    dates: list[str], prices: list[float], start: str | None, end: str | None
) -> tuple[list[str], list[float]]:
    selected = [
        (day, float(price))
        for day, price in zip(dates, prices, strict=True)
        if (start is None or day >= start) and (end is None or day <= end)
    ]
    if len(selected) < 2:
        raise ValueError("INSUFFICIENT_OBSERVATIONS")
    return [row[0] for row in selected], [row[1] for row in selected]


def analyze(args: argparse.Namespace) -> int:
    path = Path(args.asset)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("state") not in {"published", "live_api", "stale", "degraded"}:
        _json(
            {
                "contract": "kelly-cli-result",
                "mode": "historical",
                "status": "unavailable",
                "reason": "DATA_UNAVAILABLE",
                "asset": str(path),
            }
        )
        return 2
    dates, prices = _slice_prices(
        list(payload["dates"]),
        [float(value) for value in payload["prices"]],
        args.start,
        args.end,
    )
    returns = simple_returns_from_prices(prices)
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
    _json(
        {
            "contract": "kelly-cli-result",
            "mode": "historical",
            "assetId": payload.get("assetId"),
            "state": payload.get("state"),
            "period": {"start": dates[0], "end": dates[-1]},
            "returnBasis": (payload.get("metadata") or {}).get("returnBasis"),
            "metrics": metrics.as_dict(),
            "gbmKelly": gbm.as_dict(),
            "exactInSampleKelly": exact.as_dict(),
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
            "covariance": covariance.tolist(),
            "result": result.as_dict(),
            "disclaimer": "Input-driven model diagnostic, not an investment recommendation.",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (KellyLabError, ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        _json({"status": "unavailable", "reason": str(error)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
