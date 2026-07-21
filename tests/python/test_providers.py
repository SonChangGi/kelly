from __future__ import annotations

from datetime import date

import pytest

from kelly_lab.providers import KrxOfficialCsvProvider, ProviderUnavailable, TwelveDataProvider


def test_twelve_data_never_implicitly_enables_external_display() -> None:
    provider = TwelveDataProvider(api_key="not-a-real-key", external_display_approved=False)
    assert not provider.available
    with pytest.raises(ProviderUnavailable, match="LICENSE_OR_KEY"):
        provider.history("AAPL", date(2026, 1, 1), date(2026, 1, 2), adjust="all")


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
