import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from financial_provider_contract import (
    AdjustmentMode,
    DegradationInfo,
    FinancialDataKind,
    FinancialDataProvider,
    FinancialDataRecord,
    FinancialDataRequest,
    FinancialProviderResponse,
    FreshnessState,
    InvalidSymbolError,
    MarketStatus,
    PermissionDeniedError,
    PROVIDER_ERROR_TYPES,
    ProviderErrorCode,
    RateLimitedError,
    StaleDataError,
    TemporarilyUnavailableError,
    UnsupportedAssetError,
    raw_response_hash,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "financial_provider_records.json"
REQUESTED_AT = datetime(2026, 7, 31, 1, 0, 2, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 7, 31, 1, 0, 0, tzinfo=timezone.utc)
FETCHED_AT = datetime(2026, 7, 31, 1, 0, 1, tzinfo=timezone.utc)


def _record(fixture, **overrides):
    values = {
        "instrument_id": fixture["instrument_id"],
        "metric": fixture["metric"],
        "value": fixture["value"],
        "unit": fixture["unit"],
        "currency": fixture["currency"],
        "market_status": fixture["market_status"],
        "observed_at": OBSERVED_AT,
        "fetched_at": FETCHED_AT,
        "timezone": fixture["timezone"],
        "freshness_state": fixture["freshness_state"],
        "requested_as_of": REQUESTED_AT,
        "effective_from": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "effective_to": datetime(2026, 12, 31, tzinfo=timezone.utc),
        "raw_response_hash": raw_response_hash(fixture["normalized_payload"]),
        "normalized_payload": fixture["normalized_payload"],
        "adjustment": fixture["adjustment"],
        "quality_flags": tuple(fixture["quality_flags"]),
        "source_url": "https://provider.example/source",
        "provider_symbol": "provider-symbol",
        "normalizer_version": "contract-v1",
        "lineage": {"field_mapping": {"close": "last_price"}},
    }
    values.update(overrides)
    return FinancialDataRecord(**values)


def _request(fixture, *, preferred_provider_id="fixture_provider"):
    return FinancialDataRequest(
        request_id=f"request-{fixture['data_kind']}",
        endpoint=fixture["endpoint"],
        instrument_id=fixture["instrument_id"],
        metric=fixture["metric"],
        data_kind=fixture["data_kind"],
        requested_as_of=REQUESTED_AT,
        preferred_provider_id=preferred_provider_id,
        parameters={"as_of": "2026-07-31"},
    )


def _response(fixture, record, *, provider_id="fixture_provider", degradation=None):
    return FinancialProviderResponse(
        provider_id=provider_id,
        endpoint=fixture["endpoint"],
        license_profile="offline_fixture",
        request_id=f"request-{fixture['data_kind']}",
        data_kind=fixture["data_kind"],
        records=(record,),
        degradation=degradation or DegradationInfo(),
    )


class _FixtureProvider(FinancialDataProvider):
    def __init__(self, response, capabilities=None):
        self.response = response
        self._capabilities = tuple(capabilities or [response.data_kind])

    @property
    def provider_id(self):
        return "fixture_provider"

    @property
    def license_profile(self):
        return "offline_fixture"

    @property
    def capabilities(self):
        return self._capabilities

    def fetch(self, _request):
        return self.response


class FinancialProviderContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_all_data_kinds_produce_complete_evidence(self):
        expected_kinds = {item.value for item in FinancialDataKind}
        self.assertEqual({item["data_kind"] for item in self.fixtures}, expected_kinds)
        required_evidence_fields = {
            "provider_id",
            "endpoint",
            "license_profile",
            "request_id",
            "instrument_id",
            "metric",
            "value",
            "unit",
            "currency",
            "market_status",
            "observed_at",
            "fetched_at",
            "freshness_state",
            "requested_as_of",
            "effective_from",
            "effective_to",
            "raw_response_hash",
            "normalized_payload",
            "degradation",
            "timezone",
            "adjustment",
            "lineage",
        }
        for fixture in self.fixtures:
            with self.subTest(data_kind=fixture["data_kind"]):
                record = _record(fixture)
                request = _request(fixture)
                response = _response(fixture, record).validate_for(request)
                evidence = response.evidence()[0]
                self.assertTrue(required_evidence_fields <= set(evidence))
                self.assertEqual(evidence["currency"], fixture["currency"])
                self.assertEqual(evidence["timezone"], fixture["timezone"])
                self.assertEqual(evidence["adjustment"], fixture["adjustment"])
                self.assertTrue(evidence["observed_at"].endswith("Z"))
                self.assertEqual(evidence["lineage"]["normalizer_version"], "contract-v1")

    def test_null_currency_adjustment_time_and_finite_value_validation(self):
        quote = self.fixtures[0]
        fundamental = self.fixtures[2]
        self.assertIsNone(_record(fundamental).value)
        invalid_cases = (
            {"value": None, "quality_flags": ()},
            {"currency": "CN"},
            {"observed_at": OBSERVED_AT.replace(tzinfo=None)},
            {"observed_at": FETCHED_AT + timedelta(seconds=1)},
            {"effective_from": datetime(2027, 1, 1, tzinfo=timezone.utc)},
            {"value": float("nan")},
            {"raw_response_hash": "not-a-hash"},
            {"timezone": "Moon/SeaOfTranquility"},
            {"adjustment": "silently_adjusted"},
        )
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                _record(quote, **values)

    def test_provider_cannot_return_natural_language_investment_conclusion(self):
        fixture = self.fixtures[0]
        with self.assertRaisesRegex(ValueError, "investment conclusion"):
            _record(
                fixture,
                normalized_payload={"investment_advice": "立即买入"},
            )
        with self.assertRaisesRegex(ValueError, "investment conclusion"):
            _record(
                fixture,
                lineage={"nested": {"trading_decision": "buy"}},
            )

    def test_fallback_requires_explicit_degradation_lineage(self):
        fixture = self.fixtures[0]
        record = _record(fixture)
        request = _request(fixture, preferred_provider_id="akshare_cn")
        with self.assertRaisesRegex(ValueError, "explicit degradation"):
            _response(fixture, record, provider_id="tushare_cn").validate_for(request)

        degradation = DegradationInfo(
            degraded=True,
            reason="akshare temporarily unavailable",
            requested_provider_id="akshare_cn",
            actual_provider_id="tushare_cn",
            attempted_provider_ids=("akshare_cn", "tushare_cn"),
        )
        response = _response(
            fixture,
            record,
            provider_id="tushare_cn",
            degradation=degradation,
        ).validate_for(request)
        self.assertTrue(response.evidence()[0]["degradation"]["degraded"])

    def test_request_response_identity_and_provider_interface_are_enforced(self):
        fixture = self.fixtures[0]
        record = _record(fixture)
        request = _request(fixture)
        response = _response(fixture, record)
        provider = _FixtureProvider(response)
        self.assertIs(provider.fetch_validated(request), response)

        with self.assertRaisesRegex(ValueError, "request_id"):
            replace(response, request_id="different").validate_for(request)
        with self.assertRaisesRegex(ValueError, "requested_as_of"):
            replace(
                response,
                records=(replace(record, requested_as_of=REQUESTED_AT + timedelta(seconds=1)),),
            ).validate_for(request)
        with self.assertRaisesRegex(ValueError, "observed_at"):
            replace(
                response,
                records=(
                    replace(
                        record,
                        observed_at=REQUESTED_AT + timedelta(seconds=1),
                        fetched_at=REQUESTED_AT + timedelta(seconds=2),
                    ),
                ),
            ).validate_for(request)
        unsupported_request = replace(request, data_kind=FinancialDataKind.MACRO)
        with self.assertRaises(UnsupportedAssetError):
            provider.fetch_validated(unsupported_request)

    def test_six_stable_error_codes_and_retry_semantics(self):
        expected_types = {
            ProviderErrorCode.PERMISSION_DENIED.value: PermissionDeniedError,
            ProviderErrorCode.RATE_LIMITED.value: RateLimitedError,
            ProviderErrorCode.UNSUPPORTED_ASSET.value: UnsupportedAssetError,
            ProviderErrorCode.TEMPORARILY_UNAVAILABLE.value: TemporarilyUnavailableError,
            ProviderErrorCode.INVALID_SYMBOL.value: InvalidSymbolError,
            ProviderErrorCode.STALE.value: StaleDataError,
        }
        self.assertEqual(PROVIDER_ERROR_TYPES, expected_types)
        for code, error_type in expected_types.items():
            error = error_type(
                "fixture error",
                provider_id="fixture_provider",
                endpoint="quote/latest",
                request_id="request-error",
                retry_after_seconds=30 if code == "rate_limited" else None,
                details={"status": 429 if code == "rate_limited" else 400},
            )
            payload = error.to_dict()
            self.assertEqual(payload["code"], code)
            self.assertEqual(
                payload["retryable"],
                code in {"rate_limited", "temporarily_unavailable", "stale"},
            )

    def test_raw_hash_is_deterministic_and_does_not_store_raw_response(self):
        left = raw_response_hash({"b": 2, "a": 1})
        right = raw_response_hash({"a": 1, "b": 2})
        self.assertEqual(left, right)
        self.assertEqual(len(left), 64)
        evidence = _response(self.fixtures[0], _record(self.fixtures[0])).evidence()[0]
        self.assertNotIn("raw_response", evidence)


if __name__ == "__main__":
    unittest.main()
