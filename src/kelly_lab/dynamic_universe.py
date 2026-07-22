"""Key-free discovery and bounded batch publication for dynamic US assets.

The locked 50-asset catalog remains authoritative.  This module discovers a
separate research universe, validates every candidate through the single-asset
collector, and publishes an atomic ``dynamic-catalog.json`` only after the
individual normalized contracts have passed their schemas.

Nasdaq's public stock screener is the primary universe source because it
exposes comparable market-cap observations.  FinanceDataReader's US exchange
listings are a lower-assurance availability fallback; their ordering is kept
but never relabelled as a cross-exchange market-cap ranking.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import requests
from jsonschema import Draft202012Validator, FormatChecker

from .dynamic_assets import (
    DynamicAssetError,
    collect_us_asset,
    is_non_common_security_name,
    normalize_us_symbol,
    require_yahoo_public_display_approval,
)
from .free_providers import (
    FinanceDataReaderYahooProvider,
    FinvizChartProvider,
    StooqCsvProvider,
    YahooChartProvider,
)
from .providers import ProviderResponseError, ProviderUnavailable

DEFAULT_BATCH_COUNT = 250
MAX_BATCH_COUNT = 500
MAX_SYMBOL_FILE_BYTES = 256 * 1024
MAX_UNIVERSE_ROWS = 5_000
MAX_BATCH_ATTEMPTS = 1_000
PROVIDER_CIRCUIT_THRESHOLD = 5
DYNAMIC_FILENAME_PATTERN = re.compile(r"^dynamic-us-[a-z0-9-]+\.json$")
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
NASDAQ_SCREENER_PAGE = "https://www.nasdaq.com/market-activity/stocks/screener"


@dataclass(frozen=True)
class UniverseCandidate:
    """One upstream-observed ticker candidate before Yahoo identity checks."""

    symbol: str
    name: str | None
    market_cap: float | None
    source: str


@dataclass(frozen=True)
class UniverseSelection:
    """Resolved candidate pool and non-fatal discovery diagnostics."""

    candidates: tuple[UniverseCandidate, ...]
    source: str
    requested_count: int
    failures: tuple[dict[str, str], ...] = ()
    fallback_reason: str | None = None


def validate_batch_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_BATCH_COUNT:
        raise DynamicAssetError(
            "invalid_batch_count",
            f"batch count must be between 1 and {MAX_BATCH_COUNT}",
        )
    return value


def _provider_symbol(value: object) -> str:
    """Canonicalize only trusted listing-provider class-share separators."""

    raw = str(value or "").strip().upper()
    if raw.count("/") == 1 and re.fullmatch(r"[A-Z][A-Z0-9]{0,9}/[A-Z0-9]{1,5}", raw):
        raw = raw.replace("/", "-")
    return normalize_us_symbol(raw)


def _safe_name(value: object) -> str | None:
    name = " ".join(str(value or "").split())
    if not name or len(name) > 240 or any(ord(character) < 32 for character in name):
        return None
    return name


def _market_cap(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        parsed = float(value)
    else:
        compact = str(value).strip().upper().replace("$", "").replace(",", "")
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMBT]?)", compact)
        if not match:
            return None
        multiplier = {
            "": 1.0,
            "K": 1_000.0,
            "M": 1_000_000.0,
            "B": 1_000_000_000.0,
            "T": 1_000_000_000_000.0,
        }[match.group(2)]
        parsed = float(match.group(1)) * multiplier
    return parsed if math.isfinite(parsed) and parsed > 0 else None


class NasdaqScreenerUniverseProvider:
    """Read Nasdaq's keyless public stock-screener JSON."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 12,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout

    def candidates(self) -> list[UniverseCandidate]:
        try:
            response = self._session.get(
                NASDAQ_SCREENER_URL,
                params={
                    "tableonly": "true",
                    "limit": MAX_UNIVERSE_ROWS,
                    "offset": 0,
                    "download": "true",
                },
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://www.nasdaq.com",
                    "Referer": NASDAQ_SCREENER_PAGE,
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Kelly-Allocation-Lab/1.0"
                    ),
                },
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise ProviderUnavailable("NASDAQ_SCREENER_REQUEST_FAILED") from None

        status = int(getattr(response, "status_code", 0))
        if status == 429:
            raise ProviderResponseError("NASDAQ_SCREENER_RATE_LIMITED")
        if status in {401, 403}:
            raise ProviderUnavailable("NASDAQ_SCREENER_ACCESS_UNAVAILABLE")
        if status != 200:
            raise ProviderResponseError("NASDAQ_SCREENER_HTTP_ERROR")
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            raise ProviderResponseError("NASDAQ_SCREENER_PAYLOAD_INVALID") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ProviderResponseError("NASDAQ_SCREENER_ROWS_MISSING")

        observed: dict[str, UniverseCandidate] = {}
        for row in rows[:MAX_UNIVERSE_ROWS]:
            if not isinstance(row, dict):
                continue
            try:
                symbol = _provider_symbol(row.get("symbol"))
            except DynamicAssetError:
                continue
            name = _safe_name(row.get("name") or row.get("companyName"))
            market_cap = _market_cap(row.get("marketCap"))
            if name is None or market_cap is None:
                continue
            candidate = UniverseCandidate(
                symbol=symbol,
                name=name,
                market_cap=market_cap,
                source="nasdaq_screener",
            )
            prior = observed.get(symbol)
            if prior is None or float(prior.market_cap or 0) < market_cap:
                observed[symbol] = candidate

        candidates = sorted(
            observed.values(),
            key=lambda item: (-(item.market_cap or 0.0), item.symbol),
        )
        if not candidates:
            raise ProviderResponseError("NASDAQ_SCREENER_NO_VALID_ROWS")
        return candidates


class FinanceDataReaderUniverseProvider:
    """Lower-assurance US listing fallback through FinanceDataReader."""

    markets = ("NASDAQ", "NYSE", "AMEX")

    def __init__(self, *, reader_module: object | None = None) -> None:
        self._reader_module = reader_module

    def _module(self) -> object:
        if self._reader_module is not None:
            return self._reader_module
        try:
            return importlib.import_module("FinanceDataReader")
        except (ImportError, ModuleNotFoundError):
            raise ProviderUnavailable("FDR_LISTING_DEPENDENCY_UNAVAILABLE") from None

    def candidates(self) -> list[UniverseCandidate]:
        module = self._module()
        listing = getattr(module, "StockListing", None)
        if not callable(listing):
            raise ProviderUnavailable("FDR_LISTING_ADAPTER_UNAVAILABLE")

        market_rows: list[list[UniverseCandidate]] = []
        for market in self.markets:
            try:
                frame = listing(market)
                records = frame.to_dict("records")
            except Exception:  # FinanceDataReader exposes upstream errors as generic exceptions.
                continue
            parsed: list[UniverseCandidate] = []
            for row in records if isinstance(records, list) else []:
                if not isinstance(row, dict):
                    continue
                try:
                    symbol = _provider_symbol(row.get("Symbol") or row.get("symbol"))
                except DynamicAssetError:
                    continue
                name = _safe_name(row.get("Name") or row.get("name"))
                if name is None:
                    continue
                cap = None
                for field in ("MarketCap", "marketCap", "MarCap", "marketValue"):
                    cap = _market_cap(row.get(field))
                    if cap is not None:
                        break
                parsed.append(
                    UniverseCandidate(
                        symbol=symbol,
                        name=name,
                        market_cap=cap,
                        source="finance_data_reader_listing",
                    )
                )
            if parsed:
                market_rows.append(parsed)

        if not market_rows:
            raise ProviderUnavailable("FDR_LISTING_UNAVAILABLE")

        # FDR's current US listing adapter is market-value ordered within each
        # exchange but drops a comparable market-cap column.  Round-robin keeps
        # that observed order without fabricating a cross-exchange ranking.
        combined: list[UniverseCandidate] = []
        seen: set[str] = set()
        for row_index in range(max(len(rows) for rows in market_rows)):
            for rows in market_rows:
                if row_index >= len(rows):
                    continue
                candidate = rows[row_index]
                if candidate.symbol in seen:
                    continue
                seen.add(candidate.symbol)
                combined.append(candidate)
        return combined[:MAX_UNIVERSE_ROWS]


def load_symbol_file(path: Path, count: int) -> UniverseSelection:
    """Read a bounded explicit ticker file with per-entry failure isolation."""

    validate_batch_count(count)
    try:
        size = path.stat().st_size
    except OSError as error:
        raise DynamicAssetError("symbol_file_unavailable", "symbol file is unavailable") from error
    if not path.is_file() or size > MAX_SYMBOL_FILE_BYTES:
        raise DynamicAssetError(
            "symbol_file_invalid",
            "symbol file must be a regular UTF-8 file no larger than "
            f"{MAX_SYMBOL_FILE_BYTES} bytes",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DynamicAssetError("symbol_file_invalid", "symbol file must be valid UTF-8") from error

    tokens: list[str] = []
    for line in text.splitlines():
        uncommented = line.split("#", maxsplit=1)[0]
        tokens.extend(token for token in re.split(r"[,\s]+", uncommented) if token)

    candidates: list[UniverseCandidate] = []
    failures: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, token in enumerate(tokens[:MAX_UNIVERSE_ROWS], start=1):
        try:
            symbol = normalize_us_symbol(token)
        except DynamicAssetError:
            failures.append({"symbol": f"entry-{index}", "reason": "invalid_symbol"})
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        candidates.append(
            UniverseCandidate(
                symbol=symbol,
                name=None,
                market_cap=None,
                source="symbol_file",
            )
        )
    if len(tokens) > MAX_UNIVERSE_ROWS:
        failures.append({"symbol": "__universe__", "reason": "symbol_file_truncated"})
    if not candidates:
        raise DynamicAssetError("symbol_file_empty", "symbol file contains no valid unique tickers")
    requested = min(count, len(candidates))
    return UniverseSelection(
        candidates=tuple(candidates),
        source="symbol_file",
        requested_count=requested,
        failures=tuple(failures),
    )


def select_universe(
    source: str,
    count: int,
    *,
    symbols_file: Path | None = None,
    nasdaq_provider: Any | None = None,
    fdr_provider: Any | None = None,
) -> UniverseSelection:
    """Resolve one discovery source without silently changing its semantics."""

    validate_batch_count(count)
    if source not in {"auto", "nasdaq", "fdr", "file"}:
        raise DynamicAssetError("invalid_universe_source", "unsupported universe source")
    if source == "file":
        if symbols_file is None:
            raise DynamicAssetError("symbol_file_required", "--symbols-file is required")
        return load_symbol_file(symbols_file, count)
    if symbols_file is not None:
        raise DynamicAssetError(
            "unexpected_symbol_file",
            "--symbols-file can be used only with --universe file",
        )

    nasdaq = nasdaq_provider or NasdaqScreenerUniverseProvider()
    fdr = fdr_provider or FinanceDataReaderUniverseProvider()
    if source in {"auto", "nasdaq"}:
        try:
            return UniverseSelection(
                candidates=tuple(nasdaq.candidates()),
                source="nasdaq_screener",
                requested_count=count,
            )
        except (ProviderUnavailable, ProviderResponseError):
            if source == "nasdaq":
                raise DynamicAssetError(
                    "nasdaq_universe_unavailable",
                    "Nasdaq screener universe is unavailable",
                ) from None
    try:
        candidates = tuple(fdr.candidates())
    except (ProviderUnavailable, ProviderResponseError):
        raise DynamicAssetError(
            "fdr_universe_unavailable",
            "FinanceDataReader US listings are unavailable",
        ) from None
    return UniverseSelection(
        candidates=candidates,
        source="finance_data_reader_listing",
        requested_count=count,
        fallback_reason="nasdaq_screener_unavailable" if source == "auto" else None,
    )


class CircuitBreakingHistoryProvider:
    """Stop retrying an optional corroboration source after one failure."""

    def __init__(self, provider: Any, circuit_code: str) -> None:
        self._provider = provider
        self._circuit_code = circuit_code
        self.disabled = False
        self.failure_reason: str | None = None

    def history(self, *args: Any, **kwargs: Any) -> Any:
        if self.disabled:
            raise ProviderUnavailable(self._circuit_code)
        try:
            return self._provider.history(*args, **kwargs)
        except (ProviderUnavailable, ProviderResponseError) as error:
            code = str(error).upper()
            system_failure = any(
                token in code
                for token in (
                    "ACCESS",
                    "CIRCUIT",
                    "HTML_CHALLENGE",
                    "HTTP_ERROR",
                    "RATE_LIMIT",
                    "REQUEST_FAILED",
                    "TIMEOUT",
                    "UPSTREAM_UNAVAILABLE",
                )
            )
            if system_failure:
                self.disabled = True
                self.failure_reason = code.lower()
            raise


def _core_symbols(root: Path) -> set[str]:
    path = root / "data/catalog.json"
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assets = payload["assets"]
        return {
            normalize_us_symbol(item["symbol"])
            for item in assets
            if item.get("assetType") in {"equity", "etf"} and item.get("currency") == "USD"
        }
    except (DynamicAssetError, KeyError, TypeError, json.JSONDecodeError):
        raise DynamicAssetError(
            "core_catalog_invalid", "core catalog symbols are invalid"
        ) from None


def _manifest_path(root: Path, scope: str) -> Path:
    if scope not in {"local", "public"}:
        raise DynamicAssetError("invalid_cache_scope", "cache scope must be local or public")
    root_resolved = root.resolve()
    unresolved = root / (
        "var/dynamic-catalog.json" if scope == "local" else "data/dynamic-catalog.json"
    )
    if unresolved.is_symlink():
        raise DynamicAssetError("unsafe_cache_path", "dynamic manifest cannot be a symlink")
    parent = unresolved.parent.resolve()
    if not parent.is_relative_to(root_resolved) or unresolved.parent.is_symlink():
        raise DynamicAssetError("unsafe_cache_path", "dynamic manifest escaped the project root")
    candidate = (parent / unresolved.name).resolve()
    if candidate.parent != parent or not candidate.is_relative_to(root_resolved):
        raise DynamicAssetError("unsafe_cache_path", "dynamic manifest target is unsafe")
    return candidate


def _validate_manifest(root: Path, manifest: dict[str, Any]) -> None:
    schema_path = root / "schemas/dynamic-catalog.schema.json"
    if not schema_path.is_file():
        raise DynamicAssetError("schema_unavailable", "dynamic catalog schema is missing")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest),
        key=lambda error: error.json_path,
    )
    if errors:
        details = "; ".join(f"{error.json_path}: {error.message}" for error in errors[:5])
        raise DynamicAssetError("contract_invalid", details)


def _existing_manifest_assets(
    root: Path,
    manifest_path: Path,
    *,
    scope_root: Path,
) -> dict[str, dict[str, Any]]:
    """Return only schema-valid, internally matching last-good entries."""

    if not manifest_path.is_file() or manifest_path.is_symlink():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _validate_manifest(root, manifest)
        asset_schema = json.loads((root / "schemas/asset.schema.json").read_text(encoding="utf-8"))
    except (DynamicAssetError, OSError, json.JSONDecodeError):
        return {}

    dynamic_root = (scope_root / "dynamic-assets").resolve()
    preserved: dict[str, dict[str, Any]] = {}
    for entry in manifest["assets"]:
        try:
            unresolved = scope_root / entry["dataPath"]
            path = unresolved.resolve()
            if (
                path.parent != dynamic_root
                or not path.is_relative_to(scope_root.resolve())
                or unresolved.is_symlink()
                or not path.is_file()
            ):
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
            errors = list(
                Draft202012Validator(
                    asset_schema,
                    format_checker=FormatChecker(),
                ).iter_errors(document)
            )
            metadata = document["metadata"]
            quality = document["quality"]
            source = document["source"]
            dates = document["dates"]
            prices = document["prices"]
            returns = document["returns"]
            intrinsic_invalid = (
                not (len(dates) == len(prices) == len(returns))
                or len(dates) < 2
                or dates != sorted(set(dates))
                or returns[0] is not None
                or document["dataAsOf"] != dates[-1]
                or quality["observationCount"] != len(dates)
                or any(
                    not isinstance(price, int | float)
                    or isinstance(price, bool)
                    or not math.isfinite(price)
                    or price <= 0
                    for price in prices
                )
                or any(
                    not isinstance(returns[index], int | float)
                    or isinstance(returns[index], bool)
                    or not math.isfinite(returns[index])
                    or abs(returns[index] - (prices[index] / prices[index - 1] - 1.0)) > 1e-10
                    for index in range(1, len(prices))
                )
            )
            if (
                errors
                or intrinsic_invalid
                or any(
                    (
                        document["assetId"] != entry["id"],
                        document["state"] != entry["state"],
                        entry["status"] != entry["state"],
                        document["dataAsOf"] != entry["dataAsOf"],
                        quality["observationCount"] != entry["observationCount"],
                        metadata.get("catalogScope") != "dynamic",
                        metadata["symbol"] != entry["symbol"],
                        metadata["assetType"] != entry["assetType"],
                        metadata["exchange"] != entry["exchange"],
                        metadata["timezone"] != entry["timezone"],
                        metadata["returnBasis"] != entry["returnBasis"],
                        metadata.get("baseCurrency") != entry["currency"],
                        source["provider"] != entry["source"]["provider"],
                        source.get("adapter", "none") != entry["source"]["adapter"],
                    )
                )
            ):
                continue
            digest_payload = "\n".join(
                f"{day}:{float(price):.12g}"
                for day, price in zip(document["dates"], document["prices"], strict=True)
            )
            if (
                source.get("contentDigest")
                != hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
            ):
                continue
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        preserved[entry["symbol"]] = dict(entry)
    return preserved


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise DynamicAssetError("unsafe_cache_path", "dynamic manifest directory is a symlink")
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _orphan_paths(scope_root: Path, assets: list[dict[str, Any]]) -> list[Path]:
    """Resolve only owned regular JSON files that a new manifest no longer references."""

    directory = scope_root / "dynamic-assets"
    if not directory.exists():
        return []
    if directory.is_symlink():
        raise DynamicAssetError("unsafe_cache_path", "dynamic asset directory is a symlink")
    resolved_directory = directory.resolve()
    referenced = {Path(asset["dataPath"]).name for asset in assets}
    orphans: list[Path] = []
    for path in directory.iterdir():
        if (
            not DYNAMIC_FILENAME_PATTERN.fullmatch(path.name)
            or path.name in referenced
            or path.is_symlink()
            or not path.is_file()
            or path.resolve().parent != resolved_directory
        ):
            continue
        orphans.append(path)
    return sorted(orphans)


def _prune_owned_orphans(paths: list[Path], *, scope_root: Path) -> None:
    directory = (scope_root / "dynamic-assets").resolve()
    for path in paths:
        # Re-check the exact owned filename and containment immediately before
        # unlinking.  Symlinks and unrelated files are deliberately untouched.
        if (
            DYNAMIC_FILENAME_PATTERN.fullmatch(path.name)
            and not path.is_symlink()
            and path.is_file()
            and path.resolve().parent == directory
        ):
            path.unlink()


def _manifest_asset(
    candidate: UniverseCandidate,
    path: Path,
    document: dict[str, Any],
    *,
    scope_root: Path,
) -> dict[str, Any]:
    metadata = document["metadata"]
    source = document["source"]
    name = metadata.get("displayName") or candidate.name or metadata["symbol"]
    return {
        "id": document["assetId"],
        "symbol": metadata["symbol"],
        "name": name,
        "assetType": metadata["assetType"],
        "exchange": metadata["exchange"],
        "currency": "USD",
        "timezone": metadata["timezone"],
        "returnBasis": metadata["returnBasis"],
        "dataPath": path.relative_to(scope_root).as_posix(),
        "state": document["state"],
        "status": document["state"],
        "dataAsOf": document["dataAsOf"],
        "observationCount": document["quality"]["observationCount"],
        "source": {
            "provider": source["provider"],
            "adapter": source.get("adapter", "none"),
        },
    }


def preflight_public_manifest(root: Path, symbol: str) -> None:
    """Fail before a public single fetch could mutate an invalid generation."""

    require_yahoo_public_display_approval("public")
    root = root.resolve()
    manifest_path = _manifest_path(root, "public")
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _validate_manifest(root, manifest)
    except (OSError, json.JSONDecodeError, DynamicAssetError) as error:
        raise DynamicAssetError(
            "existing_manifest_invalid",
            "existing public dynamic manifest is invalid and was preserved",
        ) from error
    existing_assets = _existing_manifest_assets(
        root,
        manifest_path,
        scope_root=root / "data",
    )
    if len(existing_assets) != manifest["assetCount"]:
        raise DynamicAssetError(
            "existing_manifest_invalid",
            "existing public dynamic manifest references an invalid asset",
        )
    canonical = normalize_us_symbol(symbol)
    if canonical not in existing_assets and manifest["assetCount"] >= MAX_BATCH_COUNT:
        raise DynamicAssetError(
            "dynamic_catalog_full",
            f"public dynamic catalog is limited to {MAX_BATCH_COUNT} assets",
        )


def upsert_public_asset_manifest(
    root: Path,
    asset_path: Path,
    document: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Make one explicit public fetch discoverable without touching core 50."""

    require_yahoo_public_display_approval("public")
    root = root.resolve()
    manifest_path = _manifest_path(root, "public")
    scope_root = root / "data"
    dynamic_root = (scope_root / "dynamic-assets").resolve()
    if asset_path.is_symlink():
        raise DynamicAssetError("unsafe_cache_path", "public dynamic asset cannot be a symlink")
    resolved_asset = asset_path.resolve()
    if resolved_asset.parent != dynamic_root or not resolved_asset.is_relative_to(root):
        raise DynamicAssetError(
            "unsafe_cache_path",
            "public dynamic asset must remain below data/dynamic-assets",
        )

    existing: dict[str, Any] | None = None
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            _validate_manifest(root, existing)
        except (OSError, json.JSONDecodeError, DynamicAssetError) as error:
            raise DynamicAssetError(
                "existing_manifest_invalid",
                "existing public dynamic manifest was preserved because it is invalid",
            ) from error
        verified_existing = _existing_manifest_assets(
            root,
            manifest_path,
            scope_root=scope_root,
        )
        if len(verified_existing) != existing["assetCount"]:
            raise DynamicAssetError(
                "existing_manifest_invalid",
                "existing public dynamic manifest has an invalid referenced asset",
            )

    metadata = document["metadata"]
    candidate = UniverseCandidate(
        symbol=metadata["symbol"],
        name=metadata.get("displayName"),
        market_cap=None,
        source="manual_fetch",
    )
    entry = _manifest_asset(
        candidate,
        resolved_asset,
        document,
        scope_root=scope_root,
    )
    old_assets = list(existing["assets"]) if existing is not None else []
    replaced = False
    for index, old in enumerate(old_assets):
        if old["symbol"] == entry["symbol"] or old["id"] == entry["id"]:
            old_assets[index] = entry
            replaced = True
            break
    if not replaced:
        if len(old_assets) >= MAX_BATCH_COUNT:
            raise DynamicAssetError(
                "dynamic_catalog_full",
                f"public dynamic catalog is limited to {MAX_BATCH_COUNT} assets",
            )
        old_assets.append(entry)

    prior_requested = int(existing["requestedCount"]) if existing is not None else 0
    prior_attempted = int(existing["attemptedCount"]) if existing is not None else 0
    failures = [
        item
        for item in (existing["failures"] if existing is not None else [])
        if item["symbol"] != entry["symbol"]
    ]
    orphan_paths = _orphan_paths(scope_root, old_assets)
    manifest = {
        "schemaVersion": 1,
        "contract": "kelly-dynamic-asset-catalog",
        "generatedAt": datetime.now(UTC).isoformat(),
        "universeSource": "mixed" if existing is not None else "symbol_file",
        "universeFallbackReason": (
            existing["universeFallbackReason"] if existing is not None else None
        ),
        "requestedCount": max(prior_requested, len(old_assets)),
        "attemptedCount": min(
            MAX_UNIVERSE_ROWS,
            max(prior_attempted + 1, len(old_assets)),
        ),
        "excludedCoreCount": existing["excludedCoreCount"] if existing is not None else 0,
        "freshCount": 1,
        "preservedCount": len(old_assets) - 1,
        "prunedCount": len(orphan_paths),
        "assetCount": len(old_assets),
        "failures": failures,
        "assets": old_assets,
    }
    _validate_manifest(root, manifest)
    _atomic_write(manifest_path, manifest)
    _prune_owned_orphans(orphan_paths, scope_root=scope_root)
    return manifest_path, manifest


def collect_us_batch(
    root: Path,
    selection: UniverseSelection,
    *,
    start: date | None = None,
    end: date | None = None,
    basis_mode: str = "adjusted",
    cache_scope: str = "public",
    backfill: bool = False,
    today: date | None = None,
    yahoo_provider: Any | None = None,
    fdr_history_provider: Any | None = None,
    stooq_provider: Any | None = None,
    finviz_provider: Any | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Collect candidates independently and atomically replace one manifest."""

    requested_count = validate_batch_count(selection.requested_count)
    require_yahoo_public_display_approval(cache_scope)
    root = root.resolve()
    manifest_path = _manifest_path(root, cache_scope)
    scope_root = root / ("var" if cache_scope == "local" else "data")
    existing_assets = _existing_manifest_assets(
        root,
        manifest_path,
        scope_root=scope_root,
    )
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _validate_manifest(root, existing_manifest)
        except (OSError, json.JSONDecodeError, DynamicAssetError) as error:
            raise DynamicAssetError(
                "existing_manifest_invalid",
                "existing dynamic manifest is invalid and was preserved",
            ) from error
        if len(existing_assets) != existing_manifest["assetCount"]:
            raise DynamicAssetError(
                "existing_manifest_invalid",
                "existing dynamic manifest references an invalid asset and was preserved",
            )
    yahoo = yahoo_provider or YahooChartProvider(
        timeout=10,
        max_retries=1,
        backoff_seconds=0.25,
    )
    fdr = fdr_history_provider or FinanceDataReaderYahooProvider()
    stooq = CircuitBreakingHistoryProvider(
        stooq_provider or StooqCsvProvider(timeout=5),
        "STOOQ_BATCH_CIRCUIT_OPEN",
    )
    finviz = CircuitBreakingHistoryProvider(
        finviz_provider or FinvizChartProvider(timeout=5),
        "FINVIZ_BATCH_CIRCUIT_OPEN",
    )
    core_symbols = _core_symbols(root)

    assets: list[dict[str, Any]] = []
    failures = [dict(item) for item in selection.failures]
    attempted_count = 0
    excluded_core_count = 0
    consecutive_provider_failures = 0
    stop_reason: str | None = None
    attempt_limit = min(
        MAX_BATCH_ATTEMPTS,
        max(requested_count * 3, requested_count + 50),
    )
    seen: set[str] = set()
    for candidate in selection.candidates:
        if len(assets) >= requested_count:
            break
        if attempted_count >= attempt_limit:
            stop_reason = "attempt_limit_reached"
            break
        if candidate.symbol in seen:
            continue
        seen.add(candidate.symbol)
        if candidate.symbol in core_symbols:
            excluded_core_count += 1
            continue
        if is_non_common_security_name(candidate.name):
            failures.append({"symbol": candidate.symbol, "reason": "excluded_non_common_security"})
            continue
        attempted_count += 1
        try:
            path, document = collect_us_asset(
                root,
                candidate.symbol,
                start=start,
                end=end,
                basis_mode=basis_mode,
                cache_scope=cache_scope,
                backfill=backfill,
                today=today,
                yahoo_provider=yahoo,
                fdr_provider=fdr,
                stooq_provider=stooq,
                finviz_provider=finviz,
            )
            assets.append(_manifest_asset(candidate, path, document, scope_root=scope_root))
            consecutive_provider_failures = 0
        except DynamicAssetError as error:
            failures.append({"symbol": candidate.symbol, "reason": error.reason})
            provider_failure = (
                error.reason
                in {
                    "metadata_unavailable",
                    "provider_chain_exhausted",
                }
                and "symbol_not_found" not in str(error).lower()
            )
            consecutive_provider_failures = (
                consecutive_provider_failures + 1 if provider_failure else 0
            )
            if consecutive_provider_failures >= PROVIDER_CIRCUIT_THRESHOLD:
                stop_reason = "provider_circuit_open"
                break
        except (ProviderUnavailable, ProviderResponseError):
            failures.append({"symbol": candidate.symbol, "reason": "provider_error"})
            consecutive_provider_failures += 1
            if consecutive_provider_failures >= PROVIDER_CIRCUIT_THRESHOLD:
                stop_reason = "provider_circuit_open"
                break
        except Exception:
            # A malformed third-party frame or unexpected per-symbol response
            # must not abort already validated candidates or disclose raw text.
            failures.append({"symbol": candidate.symbol, "reason": "collection_error"})
            consecutive_provider_failures = 0

    if not assets:
        raise DynamicAssetError(
            "batch_no_assets",
            "no candidate produced a validated dynamic asset; manifest was preserved",
        )

    fresh_symbols = {asset["symbol"] for asset in assets}
    preserved_count = 0
    for candidate in selection.candidates:
        if len(assets) >= requested_count:
            break
        if candidate.symbol in fresh_symbols or candidate.symbol in core_symbols:
            continue
        prior = existing_assets.get(candidate.symbol)
        if prior is None:
            continue
        assets.append(prior)
        fresh_symbols.add(candidate.symbol)
        preserved_count += 1
        failures.append({"symbol": candidate.symbol, "reason": "preserved_last_good"})

    if stop_reason is not None:
        failures.append({"symbol": "__universe__", "reason": stop_reason})
    if len(assets) < requested_count:
        failures.append({"symbol": "__universe__", "reason": "candidate_pool_exhausted"})

    generated_at = datetime.now(UTC).isoformat()
    orphan_paths = _orphan_paths(scope_root, assets)
    manifest = {
        "schemaVersion": 1,
        "contract": "kelly-dynamic-asset-catalog",
        "generatedAt": generated_at,
        "universeSource": selection.source,
        "universeFallbackReason": selection.fallback_reason,
        "requestedCount": requested_count,
        "attemptedCount": attempted_count,
        "excludedCoreCount": excluded_core_count,
        "freshCount": len(assets) - preserved_count,
        "preservedCount": preserved_count,
        "prunedCount": len(orphan_paths),
        "assetCount": len(assets),
        "failures": failures,
        "assets": assets,
    }
    _validate_manifest(root, manifest)
    _atomic_write(manifest_path, manifest)
    _prune_owned_orphans(orphan_paths, scope_root=scope_root)
    return manifest_path, manifest
