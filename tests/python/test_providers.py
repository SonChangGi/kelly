from __future__ import annotations

from datetime import date

import pytest

from kelly_lab.providers import (
    KrxOfficialApiProvider,
    KrxOfficialCsvProvider,
    ProviderResponseError,
    ProviderUnavailable,
    TwelveDataProvider,
)


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def get(self, _url: str, **kwargs: object) -> FakeResponse:
        self.requests.append(kwargs)
        return self.responses.pop(0)


def twelve_payload(**meta_overrides: object) -> dict[str, object]:
    meta = {
        "symbol": "AAPL",
        "exchange": "NASDAQ",
        "currency": "USD",
        "exchange_timezone": "America/New_York",
        "type": "Common Stock",
        **meta_overrides,
    }
    return {
        "meta": meta,
        "values": [
            {"datetime": "2026-01-02", "close": "100"},
            {"datetime": "2026-01-05", "close": "101"},
        ],
    }


def test_twelve_data_never_implicitly_enables_external_display() -> None:
    provider = TwelveDataProvider(api_key="not-a-real-key", external_display_approved=False)
    assert not provider.available
    with pytest.raises(ProviderUnavailable, match="LICENSE_OR_KEY"):
        provider.history("AAPL", date(2026, 1, 1), date(2026, 1, 2), adjust="all")


def test_twelve_data_uses_header_exchange_and_validates_identity() -> None:
    session = FakeSession([FakeResponse(twelve_payload())])
    provider = TwelveDataProvider(
        api_key="secret-value",
        external_display_approved=True,
        session=session,
    )

    result = provider.history(
        "AAPL",
        date(2026, 1, 1),
        date(2026, 1, 5),
        adjust="all",
        exchange="NASDAQ",
        currency="USD",
        asset_type="equity",
    )

    request = session.requests[0]
    assert request["headers"] == {
        "Authorization": "apikey secret-value",
        "Accept": "application/json",
    }
    assert request["params"]["exchange"] == "NASDAQ"  # type: ignore[index]
    assert "apikey" not in request["params"]  # type: ignore[operator]
    assert result.symbol == "AAPL"
    assert result.exchange == "NASDAQ"
    assert result.currency == "USD"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"symbol": "MSFT"}, "IDENTITY_SYMBOL_MISMATCH"),
        ({"exchange": "NYSE"}, "IDENTITY_EXCHANGE_MISMATCH"),
        ({"currency": "EUR"}, "IDENTITY_CURRENCY_MISMATCH"),
    ],
)
def test_twelve_data_rejects_mislabeled_provider_metadata(
    overrides: dict[str, object], reason: str
) -> None:
    provider = TwelveDataProvider(
        api_key="secret-value",
        external_display_approved=True,
        session=FakeSession([FakeResponse(twelve_payload(**overrides))]),
    )

    with pytest.raises(ProviderResponseError, match=reason):
        provider.history(
            "AAPL",
            date(2026, 1, 1),
            date(2026, 1, 5),
            adjust="all",
            exchange="NASDAQ",
            currency="USD",
            asset_type="equity",
        )


def test_twelve_data_http_error_is_stable_and_does_not_expose_secret() -> None:
    provider = TwelveDataProvider(
        api_key="secret-value",
        external_display_approved=True,
        session=FakeSession([FakeResponse({}, status_code=401)]),
    )

    with pytest.raises(ProviderUnavailable) as captured:
        provider.history(
            "AAPL",
            date(2026, 1, 1),
            date(2026, 1, 5),
            adjust="all",
            exchange="NASDAQ",
            currency="USD",
            asset_type="equity",
        )
    assert str(captured.value) == "TWELVE_DATA_ACCESS_UNAVAILABLE"
    assert "secret-value" not in str(captured.value)


def test_krx_csv_requires_official_source() -> None:
    provider = KrxOfficialCsvProvider()
    with pytest.raises(ProviderUnavailable, match="OFFICIAL_SOURCE_REQUIRED"):
        provider.parse(
            "date,close\n2026-07-20,100\n",
            symbol="005930.KS",
            source_url="https://example.com/file.csv",
        )


def test_krx_csv_is_normalized_as_price_return() -> None:
    provider = KrxOfficialCsvProvider()
    result = provider.parse(
        "date,close\n2026-07-21,101\n2026-07-20,100\n",
        symbol="005930.KS",
        source_url="https://data.krx.co.kr/example.csv",
    )
    assert result.dates == ("2026-07-20", "2026-07-21")
    assert result.prices == (100.0, 101.0)
    assert result.return_basis == "price_return"


def test_krx_api_fetches_two_allowlisted_symbols_without_exposing_key() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "OutBlock_1": [
                        {"ISU_SRT_CD": "005930", "TDD_CLSPRC": "80,000"},
                        {"ISU_SRT_CD": "000660", "TDD_CLSPRC": "210000"},
                        {"ISU_SRT_CD": "999999", "TDD_CLSPRC": "1"},
                    ]
                }
            )
        ]
    )
    provider = KrxOfficialApiProvider(
        api_key="secret-value",
        public_display_approved=True,
        session=session,
        min_interval_seconds=0,
    )
    result = provider.history_many(["005930", "000660"], date(2026, 7, 20), date(2026, 7, 20))

    assert result["005930"].prices == (80000.0,)
    assert result["000660"].prices == (210000.0,)
    assert result["005930"].return_basis == "price_return"
    assert session.requests[0]["headers"] == {
        "AUTH_KEY": "secret-value",
        "Accept": "application/json",
    }
    assert "secret-value" not in repr(result)


def test_krx_api_rejects_contract_change() -> None:
    provider = KrxOfficialApiProvider(
        api_key="secret",
        public_display_approved=True,
        session=FakeSession([FakeResponse({"unexpected": []})]),
    )
    with pytest.raises(ProviderResponseError, match="CONTRACT_CHANGED"):
        provider.history_many(["005930"], date(2026, 7, 20), date(2026, 7, 20))


def test_krx_key_alone_does_not_enable_public_display() -> None:
    provider = KrxOfficialApiProvider(api_key="secret", public_display_approved=False)
    assert provider.configured
    assert not provider.rights_approved
    assert not provider.available
    with pytest.raises(ProviderUnavailable, match="PUBLIC_DISPLAY_RIGHTS"):
        provider.history_many(["005930"], date(2026, 7, 20), date(2026, 7, 20))
