from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from kelly_lab.cli import main


def _dates(count: int, *, start: date = date(2024, 1, 1)) -> list[str]:
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _write_asset(
    path: Path,
    asset_id: str,
    returns: list[float],
    *,
    dates: list[str] | None = None,
    base_currency: str = "USD",
) -> Path:
    prices = [100.0]
    for value in returns:
        prices.append(prices[-1] * (1.0 + value))
    asset_dates = dates or _dates(len(prices))
    is_fx = asset_id == "fx-usd-krw"
    metadata = {
        "returnBasis": "fx_rate" if is_fx else "price_return",
        "baseCurrency": "USD" if is_fx else base_currency,
    }
    if is_fx:
        metadata.update(
            {
                "assetType": "fx",
                "symbol": "USD/KRW",
                "quoteCurrency": "KRW",
            }
        )
    path.write_text(
        json.dumps(
            {
                "contract": "kelly-asset-history",
                "state": "published",
                "assetId": asset_id,
                "metadata": metadata,
                "dates": asset_dates,
                "prices": prices,
            }
        ),
        encoding="utf-8",
    )
    return path


def _read_output(capsys: object) -> dict[str, object]:
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    return json.loads(captured.out)


def test_assumptions_propagates_unavailable_status_and_exit_code(capsys) -> None:
    exit_code = main(
        [
            "assumptions",
            "--excess-return",
            "0.06",
            "--volatility",
            "0",
        ]
    )
    output = _read_output(capsys)

    assert exit_code == 2
    assert output["status"] == "unavailable"
    assert output["reason"] == "zero_volatility"
    assert output["result"]["status"] == "unavailable"  # type: ignore[index]


def test_analyze_rejects_nonpositive_price_in_claimed_available_asset(
    tmp_path: Path, capsys
) -> None:
    asset = _write_asset(
        tmp_path / "invalid-price.json",
        "invalid-price",
        [0.004 if index % 3 else -0.002 for index in range(64)],
        base_currency="KRW",
    )
    payload = json.loads(asset.read_text(encoding="utf-8"))
    payload["prices"][12] = 0.0
    asset.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(["analyze", str(asset)])
    output = _read_output(capsys)

    assert exit_code == 2
    assert output["status"] == "unavailable"
    assert output["reason"] == "invalid_return"


def test_portfolio_reports_top_level_status_and_complete_inputs(tmp_path: Path, capsys) -> None:
    correlation = tmp_path / "correlation.json"
    correlation.write_text(json.dumps([[1.0, 0.2], [0.2, 1.0]]), encoding="utf-8")

    exit_code = main(
        [
            "portfolio",
            "--excess-returns",
            "0.06,0.04",
            "--volatilities",
            "0.20,0.15",
            "--correlation",
            str(correlation),
            "--risk-free",
            "0.02",
            "--borrowing-spread",
            "0.01",
        ]
    )
    output = _read_output(capsys)

    assert exit_code == 0
    assert output["status"] in {"published", "degraded"}
    assert output["inputs"] == {
        "expectedExcessReturns": [0.06, 0.04],
        "volatilities": [0.2, 0.15],
        "correlation": [[1.0, 0.2], [0.2, 1.0]],
        "riskFreeRate": 0.02,
        "borrowingSpread": 0.01,
        "leverageCap": 3.0,
    }


def test_analyze_keeps_default_native_mode_and_links_mar_to_rf(tmp_path: Path, capsys) -> None:
    returns = [0.004 if index % 3 else -0.002 for index in range(64)]
    asset = _write_asset(tmp_path / "asset.json", "asset-a", returns)

    exit_code = main(["analyze", str(asset), "--risk-free", "0.03"])
    output = _read_output(capsys)

    assert exit_code == 0
    assert output["mode"] == "historical"
    assert output["inputs"]["currency"] == "native"  # type: ignore[index]
    assert output["inputs"]["sortinoMar"] == 0.03  # type: ignore[index]
    assert output["inputs"]["sortinoMarSource"] == "risk_free_rate"  # type: ignore[index]
    two_x = output["syntheticDailyTarget2x"]  # type: ignore[assignment]
    assert two_x["frequency"] == "daily"  # type: ignore[index]
    assert len(two_x["net_rebalanced_wealth"]) == len(returns) + 1  # type: ignore[index]


def test_analyze_krw_uses_only_prior_fx_fixes_with_five_day_cap(tmp_path: Path, capsys) -> None:
    returns = [0.003 if index % 4 else -0.002 for index in range(64)]
    asset_dates = _dates(len(returns) + 1)
    asset = _write_asset(tmp_path / "asset.json", "asset-a", returns, dates=asset_dates)
    fx_dates = asset_dates[::5]
    fx = _write_asset(
        tmp_path / "fx.json",
        "fx-usd-krw",
        [0.0005 if index % 2 else -0.0002 for index in range(len(fx_dates) - 1)],
        dates=fx_dates,
    )

    exit_code = main(["analyze", str(asset), "--currency", "krw", "--fx", str(fx)])
    output = _read_output(capsys)

    assert exit_code == 0
    assert output["inputs"]["currency"] == "krw"  # type: ignore[index]
    alignment = output["fxAlignment"]  # type: ignore[assignment]
    assert alignment["status"] == "published"  # type: ignore[index]
    assert max(alignment["lag_days"]) <= 5  # type: ignore[arg-type,index]
    assert all(reason is None for reason in alignment["reasons"])  # type: ignore[index]


def test_analyze_krw_fails_closed_when_fx_is_too_stale(tmp_path: Path, capsys) -> None:
    asset_dates = _dates(10, start=date(2024, 1, 10))
    asset = _write_asset(
        tmp_path / "asset.json", "asset-a", [0.01, -0.005] * 4 + [0.002], dates=asset_dates
    )
    fx = _write_asset(
        tmp_path / "fx.json",
        "fx-usd-krw",
        [],
        dates=["2024-01-01"],
    )

    exit_code = main(["analyze", str(asset), "--currency", "krw", "--fx", str(fx)])
    output = _read_output(capsys)

    assert exit_code == 2
    assert output["status"] == "unavailable"
    assert output["reason"] == "fx_too_stale"


def test_analyze_krw_asset_does_not_require_fx(tmp_path: Path, capsys) -> None:
    returns = [0.003 if index % 4 else -0.002 for index in range(64)]
    asset = _write_asset(
        tmp_path / "krw-asset.json",
        "krw-asset",
        returns,
        base_currency="KRW",
    )

    exit_code = main(["analyze", str(asset), "--currency", "krw"])
    output = _read_output(capsys)

    assert exit_code == 0
    assert output["inputs"]["currency"] == "krw"  # type: ignore[index]
    assert output["fxAlignment"]["conversionApplied"] is False  # type: ignore[index]
    assert output["fxAlignment"]["baseCurrency"] == "KRW"  # type: ignore[index]


def test_analyze_usd_krw_quote_does_not_convert_the_rate_twice(tmp_path: Path, capsys) -> None:
    fx = _write_asset(
        tmp_path / "fx.json",
        "fx-usd-krw",
        [0.001 if index % 4 else -0.0005 for index in range(64)],
    )

    exit_code = main(["analyze", str(fx), "--currency", "krw"])
    output = _read_output(capsys)

    assert exit_code == 0
    assert output["fxAlignment"]["conversionApplied"] is False  # type: ignore[index]
    assert output["fxAlignment"]["baseCurrency"] == "KRW"  # type: ignore[index]


def test_analyze_rejects_non_fx_asset_as_usd_krw_alignment_source(tmp_path: Path, capsys) -> None:
    returns = [0.003 if index % 4 else -0.002 for index in range(64)]
    asset = _write_asset(tmp_path / "asset.json", "asset-a", returns)
    wrong_fx = _write_asset(tmp_path / "wrong-fx.json", "not-fx", returns)

    exit_code = main(["analyze", str(asset), "--currency", "krw", "--fx", str(wrong_fx)])
    output = _read_output(capsys)

    assert exit_code == 2
    assert output["reason"] == "fx_missing"


def test_analyze_fails_closed_with_fewer_than_sixty_returns(tmp_path: Path, capsys) -> None:
    asset = _write_asset(
        tmp_path / "short-history.json",
        "short-history",
        [0.004 if index % 3 else -0.002 for index in range(59)],
        base_currency="KRW",
    )

    exit_code = main(["analyze", str(asset)])
    output = _read_output(capsys)

    assert exit_code == 2
    assert output["status"] == "unavailable"
    assert output["reason"] == "insufficient_observations"


def test_portfolio_history_uses_common_dates_and_returns_both_optimizers(
    tmp_path: Path, capsys
) -> None:
    first_returns = [0.006 if index % 5 else -0.004 for index in range(70)]
    second_returns = [0.004 if index % 4 else -0.003 for index in range(70)]
    first = _write_asset(tmp_path / "first.json", "first", first_returns, base_currency="KRW")
    second = _write_asset(tmp_path / "second.json", "second", second_returns, base_currency="KRW")

    exit_code = main(["portfolio-history", str(first), str(second), "--risk-free", "0.02"])
    output = _read_output(capsys)

    assert exit_code == 0
    assert output["commonObservations"] == 70
    estimates = output["historicalEstimates"]  # type: ignore[assignment]
    assert len(estimates["covariance"]) == 2  # type: ignore[index]
    assert len(estimates["correlation"]) == 2  # type: ignore[index]
    assert output["gbmKelly"]["status"] in {"published", "degraded"}  # type: ignore[index]
    assert output["exactInSampleKelly"]["status"] == "published"  # type: ignore[index]


def test_portfolio_history_converts_mixed_usd_and_krw_assets(tmp_path: Path, capsys) -> None:
    asset_dates = _dates(71)
    usd = _write_asset(
        tmp_path / "usd.json",
        "usd",
        [0.005 if index % 4 else -0.003 for index in range(70)],
        dates=asset_dates,
        base_currency="USD",
    )
    krw = _write_asset(
        tmp_path / "krw.json",
        "krw",
        [0.004 if index % 5 else -0.002 for index in range(70)],
        dates=asset_dates,
        base_currency="KRW",
    )
    fx_dates = asset_dates[::5]
    fx = _write_asset(
        tmp_path / "fx.json",
        "fx-usd-krw",
        [0.0005 if index % 2 else -0.0002 for index in range(len(fx_dates) - 1)],
        dates=fx_dates,
    )

    exit_code = main(["portfolio-history", str(usd), str(krw), "--fx", str(fx)])
    output = _read_output(capsys)

    assert exit_code == 0
    assert output["currency"] == "KRW"
    alignment = output["fxAlignment"]  # type: ignore[assignment]
    assert alignment["required"] is True  # type: ignore[index]
    by_id = {row["assetId"]: row for row in alignment["assets"]}  # type: ignore[index]
    assert by_id["usd"]["conversionApplied"] is True
    assert by_id["usd"]["maxObservedLagDays"] <= 5
    assert by_id["krw"]["conversionApplied"] is False


def test_portfolio_history_usd_asset_requires_fx(tmp_path: Path, capsys) -> None:
    usd = _write_asset(
        tmp_path / "usd.json",
        "usd",
        [0.005 if index % 4 else -0.003 for index in range(70)],
    )
    krw = _write_asset(
        tmp_path / "krw.json",
        "krw",
        [0.004 if index % 5 else -0.002 for index in range(70)],
        base_currency="KRW",
    )

    exit_code = main(["portfolio-history", str(usd), str(krw)])
    output = _read_output(capsys)

    assert exit_code == 2
    assert output["reason"] == "fx_missing"


def test_portfolio_history_fails_closed_for_stale_fx(tmp_path: Path, capsys) -> None:
    asset_dates = _dates(71, start=date(2024, 1, 10))
    usd = _write_asset(
        tmp_path / "usd.json",
        "usd",
        [0.005 if index % 4 else -0.003 for index in range(70)],
        dates=asset_dates,
    )
    krw = _write_asset(
        tmp_path / "krw.json",
        "krw",
        [0.004 if index % 5 else -0.002 for index in range(70)],
        dates=asset_dates,
        base_currency="KRW",
    )
    fx = _write_asset(
        tmp_path / "fx.json",
        "fx-usd-krw",
        [],
        dates=["2024-01-01"],
    )

    exit_code = main(["portfolio-history", str(usd), str(krw), "--fx", str(fx)])
    output = _read_output(capsys)

    assert exit_code == 2
    assert output["reason"] == "fx_too_stale"


def test_portfolio_history_fails_closed_below_sixty_common_returns(tmp_path: Path, capsys) -> None:
    first = _write_asset(tmp_path / "first.json", "first", [0.01, -0.005] * 10, base_currency="KRW")
    second = _write_asset(
        tmp_path / "second.json", "second", [0.002, -0.001] * 10, base_currency="KRW"
    )

    exit_code = main(["portfolio-history", str(first), str(second)])
    output = _read_output(capsys)

    assert exit_code == 2
    assert output["reason"] == "insufficient_common_observations"
    assert output["commonObservations"] == 20


def test_rebalance_command_calls_path_engine_and_reports_costs(tmp_path: Path, capsys) -> None:
    path = tmp_path / "rebalance.json"
    path.write_text(
        json.dumps(
            {
                "dates": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
                "returnsMatrix": [[0.10, 0.0], [-0.05, 0.03], [0.02, -0.01]],
                "targetWeights": [0.6, 0.4],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "rebalance",
            str(path),
            "--frequency",
            "daily",
            "--cost-bps",
            "12",
            "--risk-free",
            "0.02",
            "--borrowing-spread",
            "0.01",
        ]
    )
    output = _read_output(capsys)

    assert exit_code == 0
    assert output["status"] == "published"
    assert output["inputs"]["oneWayCostBps"] == 12.0  # type: ignore[index]
    assert output["result"]["frequency"] == "daily"  # type: ignore[index]
    assert output["result"]["trading_cost_paid"] > 0  # type: ignore[index]


def test_rebalance_invalid_frequency_is_machine_readable(tmp_path: Path, capsys) -> None:
    path = tmp_path / "rebalance.json"
    path.write_text(
        json.dumps(
            {
                "dates": ["2024-01-01", "2024-01-02"],
                "returnsMatrix": [[0.01]],
                "targetWeights": [1.0],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["rebalance", str(path), "--frequency", "hourly"])
    output = _read_output(capsys)

    assert exit_code == 2
    assert output["status"] == "unavailable"
    assert output["reason"] == "invalid_frequency"
