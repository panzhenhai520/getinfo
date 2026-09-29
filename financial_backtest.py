#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Point-in-time backtesting over persisted financial snapshots.

This module deliberately stays inside the existing process, SQLite database and
``paper_backtest`` worker job.  It never fetches data, calls a model, converts
currencies implicitly, or exposes a real-order seam.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from financial_config import require_financial_product_capability


FINANCIAL_BACKTEST_VERSION = "financial-point-in-time-backtest-v1"
SUPPORTED_STRATEGIES = frozenset({"buy_and_hold_v1", "sma_cross_v1"})
BLOCKED_MARKET_STATES = frozenset({"suspended", "halted", "delisted"})


class BacktestError(ValueError):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)
        self.retryable = False


def _json_object(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x1f".join(str(item or "") for item in parts).encode("utf-8")
    ).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise BacktestError(f"{name} 无效", error_code=f"invalid_{name}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BacktestError(f"{name} 无效", error_code=f"invalid_{name}") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise BacktestError(f"{name} 无效", error_code=f"invalid_{name}")
    return result


def _number(value: object, name: str, *, positive: bool = False) -> float:
    result = _decimal(value, name, positive=positive)
    return float(result)


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.00000001")))


def _utc(value: object, name: str, *, timezone_name: str = "UTC") -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise BacktestError(f"{name} 缺失", error_code=f"{name}_required")
    try:
        if len(raw) == 8 and raw.isdigit():
            parsed_date = datetime.strptime(raw, "%Y%m%d").date()
            parsed = datetime.combine(parsed_date, time(16, 0))
        elif len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            parsed = datetime.combine(date.fromisoformat(raw), time(16, 0))
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BacktestError(f"{name} 无效", error_code=f"invalid_{name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name or "UTC"))
        except ZoneInfoNotFoundError as exc:
            raise BacktestError("快照时区无效", error_code="invalid_snapshot_timezone") from exc
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _date(value: object, name: str) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise BacktestError(f"{name} 必须是 ISO 日期", error_code=f"invalid_{name}") from exc


@dataclass(frozen=True)
class PointInTimeBar:
    instrument_id: int
    snapshot_id: int
    snapshot_sha256: str
    observed_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    market_status: str
    adjustment: str


@dataclass(frozen=True)
class CorporateAction:
    instrument_id: int
    action_key: str
    action_type: str
    effective_at: datetime
    available_at: datetime
    value: Decimal
    currency: str


class FinancialPointInTimeBacktester:
    """Deterministic, no-lookahead backtester using only pinned SQLite rows."""

    def __init__(self, database, *, settings=None, clock=None):
        self.database = database
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._fallback_lock = threading.RLock()

    @property
    def connection(self):
        if hasattr(self.database, "_ensure_connection"):
            self.database._ensure_connection()
            return self.database.connection
        return self.database

    @property
    def lock(self):
        return getattr(self.database, "lock", self._fallback_lock)

    def _require(self, pack_id: str) -> dict:
        return require_financial_product_capability(
            "backtesting", str(pack_id or ""), settings=self.settings
        )

    def _instrument(self, instrument_id: int) -> dict:
        row = self.connection.execute(
            """
            SELECT id, canonical_symbol, display_name, asset_type, market,
                   exchange, currency, listing_status, listed_at, delisted_at
            FROM financial_instruments WHERE id=?
            """,
            (int(instrument_id),),
        ).fetchone()
        if row is None:
            raise BacktestError("回测标的不存在", error_code="instrument_not_found")
        return {
            "instrument_id": int(row[0]), "canonical_symbol": str(row[1]),
            "display_name": str(row[2]), "asset_type": str(row[3]).casefold(),
            "market": str(row[4]), "exchange": str(row[5]),
            "currency": str(row[6]).upper(), "listing_status": str(row[7]).casefold(),
            "listed_at": str(row[8] or ""), "delisted_at": str(row[9] or ""),
        }

    def _snapshot_row(self, snapshot_id: int) -> dict:
        row = self.connection.execute(
            """
            SELECT snapshot.id, snapshot.instrument_id, snapshot.universe_id,
                   snapshot.data_type, snapshot.interval_code,
                   snapshot.observed_at, snapshot.fetched_at,
                   snapshot.market_status, snapshot.currency, snapshot.timezone,
                   snapshot.quality_status, snapshot.payload_json,
                   snapshot.payload_sha256, snapshot.source_url,
                   profile.provider_key
            FROM financial_data_snapshots snapshot
            JOIN financial_provider_profiles profile
              ON profile.id=snapshot.provider_profile_id
            WHERE snapshot.id=?
            """,
            (int(snapshot_id),),
        ).fetchone()
        if row is None:
            raise BacktestError("回测数据快照不存在", error_code="snapshot_not_found")
        payload_text = str(row[11] or "")
        digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        if digest != str(row[12] or ""):
            raise BacktestError("回测快照 hash 校验失败", error_code="snapshot_integrity_failed")
        quality = str(row[10] or "").casefold()
        if not quality.startswith(("normalized", "verified", "available")):
            raise BacktestError("回测快照未经规范化", error_code="snapshot_not_normalized")
        return {
            "snapshot_id": int(row[0]),
            "instrument_id": int(row[1]) if row[1] is not None else None,
            "universe_id": int(row[2]) if row[2] is not None else None,
            "data_type": str(row[3]), "interval_code": str(row[4] or ""),
            "observed_at": str(row[5]), "fetched_at": str(row[6]),
            "market_status": str(row[7]).casefold(), "currency": str(row[8]).upper(),
            "timezone": str(row[9] or "UTC"), "quality_status": quality,
            "payload": _json_object(payload_text), "payload_sha256": str(row[12]),
            "source_url": str(row[13]), "provider_key": str(row[14]),
        }

    @staticmethod
    def _series(payload: Mapping[str, object]) -> Sequence:
        normalized = _json_object(payload.get("normalized_payload"))
        candidates = (
            normalized.get("bars"), payload.get("bars"), normalized.get("navs"),
            payload.get("navs"), normalized.get("nav_series"), payload.get("nav_series"),
            payload.get("value"),
        )
        for value in candidates:
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                return value
        return ()

    def _load_snapshot_data(
        self, snapshot_ids: Sequence[int], *, data_cutoff_at: datetime
    ) -> tuple[dict[int, list[PointInTimeBar]], list[CorporateAction], list[dict]]:
        bars: dict[int, list[PointInTimeBar]] = defaultdict(list)
        actions: list[CorporateAction] = []
        manifest: list[dict] = []
        seen = set()
        for raw_id in snapshot_ids:
            snapshot_id = int(raw_id)
            if snapshot_id in seen:
                continue
            seen.add(snapshot_id)
            snapshot = self._snapshot_row(snapshot_id)
            if snapshot["instrument_id"] is None:
                raise BacktestError(
                    "回测行情快照必须关联具体标的", error_code="instrument_snapshot_required"
                )
            fetched_at = _utc(snapshot["fetched_at"], "snapshot_fetched_at")
            if fetched_at > data_cutoff_at:
                raise BacktestError(
                    "数据快照晚于本次数据截止版本", error_code="snapshot_after_data_cutoff"
                )
            payload = snapshot["payload"]
            normalized = _json_object(payload.get("normalized_payload"))
            adjustment = str(
                normalized.get("adjustment") or payload.get("adjustment") or "raw"
            ).casefold()
            rows = self._series(payload)
            for index, raw in enumerate(rows):
                if not isinstance(raw, Mapping):
                    continue
                item = dict(raw)
                observed_value = (
                    item.get("observed_at") or item.get("time") or item.get("date")
                    or item.get("trade_date")
                )
                observed_at = _utc(
                    observed_value, "bar_observed_at", timezone_name=snapshot["timezone"]
                )
                available_at = _utc(
                    item.get("available_at") or item.get("published_at") or observed_value,
                    "bar_available_at", timezone_name=snapshot["timezone"],
                )
                nav = item.get("nav")
                close_value = item.get("close", nav)
                open_value = item.get("open", close_value)
                high_value = item.get("high", close_value)
                low_value = item.get("low", close_value)
                close = _decimal(close_value, "bar_close", positive=True)
                open_price = _decimal(open_value, "bar_open", positive=True)
                high = _decimal(high_value, "bar_high", positive=True)
                low = _decimal(low_value, "bar_low", positive=True)
                if high < max(open_price, close) or low > min(open_price, close) or high < low:
                    raise BacktestError("OHLC 数据不一致", error_code="invalid_ohlc")
                volume_value = item.get("volume")
                volume = None
                if volume_value not in (None, ""):
                    volume = _decimal(volume_value, "bar_volume")
                    if volume < 0:
                        raise BacktestError("成交量不能为负", error_code="invalid_bar_volume")
                bars[int(snapshot["instrument_id"])].append(
                    PointInTimeBar(
                        instrument_id=int(snapshot["instrument_id"]), snapshot_id=snapshot_id,
                        snapshot_sha256=snapshot["payload_sha256"], observed_at=observed_at,
                        available_at=available_at, open=open_price, high=high, low=low,
                        close=close, volume=volume,
                        market_status=str(item.get("market_status") or "trading").casefold(),
                        adjustment=adjustment,
                    )
                )
            raw_actions = normalized.get("corporate_actions", payload.get("corporate_actions", ()))
            if (
                adjustment == "raw"
                and isinstance(raw_actions, Sequence)
                and not isinstance(raw_actions, (str, bytes, bytearray))
            ):
                for index, raw_action in enumerate(raw_actions):
                    if not isinstance(raw_action, Mapping):
                        continue
                    action = dict(raw_action)
                    action_type = str(action.get("type") or action.get("action_type") or "").casefold()
                    if action_type not in {"split", "dividend"}:
                        continue
                    effective_value = action.get("effective_at") or action.get("ex_date")
                    effective_at = _utc(
                        effective_value, "corporate_action_effective_at",
                        timezone_name=snapshot["timezone"],
                    )
                    available_at = _utc(
                        action.get("available_at") or action.get("announced_at") or effective_value,
                        "corporate_action_available_at", timezone_name=snapshot["timezone"],
                    )
                    amount = action.get("ratio") if action_type == "split" else action.get("amount")
                    value = _decimal(amount, f"{action_type}_value", positive=True)
                    actions.append(
                        CorporateAction(
                            instrument_id=int(snapshot["instrument_id"]),
                            action_key=str(action.get("id") or f"{snapshot_id}:{index}:{action_type}"),
                            action_type=action_type, effective_at=effective_at,
                            available_at=available_at, value=value,
                            currency=str(action.get("currency") or snapshot["currency"]).upper(),
                        )
                    )
            manifest.append(
                {
                    "snapshot_id": snapshot_id, "instrument_id": snapshot["instrument_id"],
                    "payload_sha256": snapshot["payload_sha256"],
                    "fetched_at": snapshot["fetched_at"], "provider_key": snapshot["provider_key"],
                    "source_url": snapshot["source_url"], "row_count": len(rows),
                    "adjustment": adjustment,
                    "embedded_corporate_action_count": (
                        len(raw_actions)
                        if isinstance(raw_actions, Sequence)
                        and not isinstance(raw_actions, (str, bytes, bytearray))
                        else 0
                    ),
                    "corporate_actions_applied_separately": adjustment == "raw",
                }
            )
        if not manifest:
            raise BacktestError("必须指定已入库历史快照", error_code="snapshot_ids_required")
        for values in bars.values():
            values.sort(key=lambda item: (item.observed_at, item.available_at, item.snapshot_id))
        actions.sort(key=lambda item: (item.effective_at, item.available_at, item.action_key))
        manifest.sort(key=lambda item: int(item["snapshot_id"]))
        return dict(bars), actions, manifest

    @staticmethod
    def _known_bars_as_of(
        values: Sequence[PointInTimeBar], as_of: datetime
    ) -> list[PointInTimeBar]:
        selected: dict[datetime, PointInTimeBar] = {}
        for item in values:
            if item.observed_at > as_of or item.available_at > as_of:
                continue
            previous = selected.get(item.observed_at)
            if previous is None or (item.available_at, item.snapshot_id) > (
                previous.available_at, previous.snapshot_id
            ):
                selected[item.observed_at] = item
        return [selected[key] for key in sorted(selected)]

    @staticmethod
    def _bar_at(
        values: Sequence[PointInTimeBar], observed_at: datetime
    ) -> PointInTimeBar | None:
        candidates = [
            item for item in values
            if item.observed_at == observed_at and item.available_at <= observed_at
        ]
        return max(candidates, key=lambda item: (item.available_at, item.snapshot_id)) if candidates else None

    def _universe_contract(self, universe_id: int) -> dict:
        row = self.connection.execute(
            """
            SELECT id, universe_key, display_name, universe_type, market,
                   definition_json, source_provider_key, constituent_as_of
            FROM financial_universes WHERE id=?
            """,
            (int(universe_id),),
        ).fetchone()
        if row is None:
            raise BacktestError("回测范围不存在", error_code="universe_not_found")
        members = self.connection.execute(
            """
            SELECT instrument_id, weight, effective_from, effective_to,
                   source_observed_at, metadata_json
            FROM financial_universe_members WHERE universe_id=?
            ORDER BY effective_from, instrument_id
            """,
            (int(universe_id),),
        ).fetchall()
        return {
            "universe_id": int(row[0]), "universe_key": str(row[1]),
            "display_name": str(row[2]), "universe_type": str(row[3]),
            "market": str(row[4]), "definition": _json_object(row[5]),
            "source_provider_key": str(row[6]), "constituent_as_of": str(row[7] or ""),
            "members": [
                {
                    "instrument_id": int(member[0]),
                    "weight": float(member[1]) if member[1] is not None else None,
                    "effective_from": str(member[2]), "effective_to": str(member[3] or ""),
                    "source_observed_at": str(member[4] or ""),
                    "metadata": _json_object(member[5]),
                }
                for member in members
            ],
        }

    @staticmethod
    def _member_known(member: Mapping[str, object], as_of: datetime) -> bool:
        day = as_of.date().isoformat()
        if str(member.get("effective_from") or "") > day:
            return False
        if member.get("effective_to") and str(member["effective_to"]) <= day:
            return False
        observed = str(member.get("source_observed_at") or "")
        if not observed:
            return False
        try:
            return _utc(observed, "member_source_observed_at") <= as_of
        except BacktestError:
            return False

    def _existing(self, run_id: str, owner_user_id: str, fingerprint: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT id, strategy_key, model_version, scope_type, instrument_id,
                   universe_id, start_date, end_date, initial_capital,
                   config_json, status, data_cutoff_at, last_error,
                   created_at, started_at, completed_at
            FROM backtest_runs WHERE id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        config = _json_object(row[9])
        if str(config.get("owner_user_id") or "") != owner_user_id:
            raise PermissionError("无权访问该回测")
        if str(config.get("request_fingerprint") or "") != fingerprint:
            raise BacktestError("回测幂等键参数冲突", error_code="backtest_idempotency_conflict")
        if str(row[10]) == "completed":
            trades = self.connection.execute(
                """
                SELECT instrument_id, side, quantity, price, fee,
                       signal_at, executed_at, reason_json
                FROM backtest_trades WHERE backtest_run_id=? ORDER BY id
                """,
                (run_id,),
            ).fetchall()
            return {
                "backtest_run_id": run_id, "status": "completed", "idempotent": True,
                "strategy_key": str(row[1]), "strategy_version": str(row[2]),
                "scope_type": str(row[3]), "instrument_id": row[4], "universe_id": row[5],
                "start_date": str(row[6]), "end_date": str(row[7]),
                "initial_capital": float(row[8]), "data_cutoff_at": str(row[11]),
                "config": config,
                "trades": [
                    {
                        "instrument_id": int(item[0]), "side": str(item[1]),
                        "quantity": float(item[2]), "price": float(item[3]),
                        "fee": float(item[4]), "signal_at": str(item[5] or ""),
                        "executed_at": str(item[6]), "reason": _json_object(item[7]),
                    }
                    for item in trades
                ],
                "execution_mode": "paper", "real_order_execution": False,
            }
        return None

    def run(
        self, *, owner_user_id: object, industry_pack_id: str,
        idempotency_key: str, strategy_key: str, strategy_version: str,
        scope_type: str, start_date: object, end_date: object,
        initial_capital: object, base_currency: str, snapshot_ids: Sequence[int],
        instrument_id: int | None = None, universe_id: int | None = None,
        data_cutoff_at: object = None, strategy_parameters: Mapping | None = None,
        fee_rate: object = "0.001", slippage_bps: object = "0",
        benchmark_snapshot_id: int | None = None, random_seed: int = 0,
    ) -> dict:
        self._require(industry_pack_id)
        owner = str(owner_user_id or "").strip()
        if not owner:
            raise PermissionError("回测必须关联登录用户")
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 160:
            raise BacktestError("回测幂等键无效", error_code="invalid_idempotency_key")
        strategy = str(strategy_key or "").casefold()
        version = str(strategy_version or "").strip()[:160]
        if strategy not in SUPPORTED_STRATEGIES:
            raise BacktestError("不支持的回测策略", error_code="unsupported_strategy")
        if not version:
            raise BacktestError("策略版本不能为空", error_code="strategy_version_required")
        scope = str(scope_type or "").casefold()
        if scope not in {"instrument", "universe"}:
            raise BacktestError("回测范围无效", error_code="invalid_scope_type")
        if (scope == "instrument") != (instrument_id is not None and universe_id is None):
            raise BacktestError("单标的范围参数不一致", error_code="invalid_instrument_scope")
        if (scope == "universe") != (universe_id is not None and instrument_id is None):
            raise BacktestError("组合范围参数不一致", error_code="invalid_universe_scope")
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start > end:
            raise BacktestError("开始日期晚于结束日期", error_code="invalid_date_range")
        capital = _decimal(initial_capital, "initial_capital", positive=True)
        currency = str(base_currency or "").upper()
        if len(currency) != 3 or not currency.isalpha():
            raise BacktestError("基础币种无效", error_code="invalid_base_currency")
        fee = _decimal(fee_rate, "fee_rate")
        slip = _decimal(slippage_bps, "slippage_bps") / Decimal("10000")
        if fee < 0 or fee > Decimal("0.1") or slip < 0 or slip > Decimal("0.1"):
            raise BacktestError("费用或滑点参数无效", error_code="invalid_execution_cost")
        cutoff = (
            _utc(data_cutoff_at, "data_cutoff_at")
            if data_cutoff_at not in (None, "") else self.clock().astimezone(timezone.utc)
        )
        if cutoff > self.clock().astimezone(timezone.utc) + timedelta(seconds=1):
            raise BacktestError("数据截止时间不能位于未来", error_code="future_data_cutoff")
        if strategy_parameters is not None and not isinstance(strategy_parameters, Mapping):
            raise BacktestError("策略参数必须是对象", error_code="invalid_strategy_parameters")
        parameters = dict(strategy_parameters or {})
        short_window = int(parameters.get("short_window", 2))
        long_window = int(parameters.get("long_window", 3))
        if strategy == "sma_cross_v1" and not (1 <= short_window < long_window <= 500):
            raise BacktestError("均线窗口无效", error_code="invalid_sma_windows")
        if (
            not isinstance(snapshot_ids, Sequence)
            or isinstance(snapshot_ids, (str, bytes, bytearray))
            or not snapshot_ids
        ):
            raise BacktestError("必须指定快照 ID 列表", error_code="snapshot_ids_required")
        if isinstance(random_seed, bool):
            raise BacktestError("随机种子无效", error_code="invalid_random_seed")
        try:
            seed = int(random_seed)
        except (TypeError, ValueError) as exc:
            raise BacktestError("随机种子无效", error_code="invalid_random_seed") from exc
        if abs(seed) > 2147483647:
            raise BacktestError("随机种子超出范围", error_code="invalid_random_seed")
        request_contract = {
            "owner_user_id": owner, "industry_pack_id": str(industry_pack_id),
            "strategy_key": strategy, "strategy_version": version, "scope_type": scope,
            "instrument_id": instrument_id, "universe_id": universe_id,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "initial_capital": _money(capital), "base_currency": currency,
            "snapshot_ids": sorted({int(item) for item in snapshot_ids}),
            "benchmark_snapshot_id": int(benchmark_snapshot_id) if benchmark_snapshot_id else None,
            "data_cutoff_at": _utc_text(cutoff), "strategy_parameters": parameters,
            "fee_rate": str(fee), "slippage_bps": str(slippage_bps),
            "random_seed": seed,
        }
        fingerprint = _sha(request_contract)
        run_id = _stable_id("backtest", owner, key)
        existing = self._existing(run_id, owner, fingerprint)
        if existing is not None:
            return existing

        bars, actions, manifest = self._load_snapshot_data(
            request_contract["snapshot_ids"], data_cutoff_at=cutoff
        )
        benchmark_manifest = None
        if benchmark_snapshot_id is not None:
            _, _, items = self._load_snapshot_data(
                [int(benchmark_snapshot_id)], data_cutoff_at=cutoff
            )
            benchmark_manifest = items[0]

        universe = None
        if scope == "instrument":
            target_ids = {int(instrument_id)}
        else:
            universe = self._universe_contract(int(universe_id))
            relevant_members = [
                item for item in universe["members"]
                if str(item.get("effective_from") or "") <= end.isoformat()
                and (
                    not item.get("effective_to")
                    or str(item["effective_to"]) > start.isoformat()
                )
            ]
            universe = {**universe, "members": relevant_members}
            target_ids = {int(item["instrument_id"]) for item in relevant_members}
            if not target_ids:
                raise BacktestError(
                    "日期范围内没有可验证的范围成分", error_code="universe_members_missing"
                )
        unexpected = sorted(set(bars) - target_ids)
        if unexpected:
            raise BacktestError("快照包含范围外标的", error_code="snapshot_scope_mismatch")
        adjustments: dict[int, set[str]] = defaultdict(set)
        for item in manifest:
            adjustments[int(item["instrument_id"])].add(str(item["adjustment"]))
        if any(len(values) > 1 for values in adjustments.values()):
            raise BacktestError(
                "同一标的不能混用复权口径", error_code="mixed_adjustment_modes"
            )
        instruments = {item: self._instrument(item) for item in sorted(target_ids)}
        currencies = sorted(
            {item["currency"] for item in instruments.values() if item["currency"]}
        )
        if currencies != [currency]:
            raise BacktestError(
                "回测不执行隐式外汇换算，请按币种拆分范围",
                error_code="currency_mismatch",
            )
        for snapshot in manifest:
            if snapshot["instrument_id"] in instruments:
                snapshot_currency = self._snapshot_row(snapshot["snapshot_id"])["currency"]
                if snapshot_currency and snapshot_currency != currency:
                    raise BacktestError("快照币种不匹配", error_code="snapshot_currency_mismatch")

        start_at = datetime.combine(start, time.min, tzinfo=timezone.utc)
        end_at = datetime.combine(end, time.max, tzinfo=timezone.utc)
        filtered = {
            instrument: [item for item in values if start_at <= item.observed_at <= end_at]
            for instrument, values in bars.items()
        }
        event_times = sorted({item.observed_at for values in filtered.values() for item in values})
        if not event_times:
            raise BacktestError("日期范围内没有可用历史数据", error_code="historical_data_missing")

        member_manifest = list(universe["members"]) if universe else []

        def active(instrument: int, as_of: datetime) -> bool:
            if scope == "instrument":
                return instrument == int(instrument_id)
            return any(
                int(item["instrument_id"]) == instrument and self._member_known(item, as_of)
                for item in member_manifest
            )

        last_active_event: dict[int, datetime] = {}
        for instrument in sorted(target_ids):
            candidates = [
                event_at for event_at in event_times
                if active(instrument, event_at)
                and (bar := self._bar_at(filtered.get(instrument, ()), event_at)) is not None
                and bar.market_status not in BLOCKED_MARKET_STATES
            ]
            if candidates:
                last_active_event[instrument] = max(candidates)

        cash = capital
        positions: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        average_cost: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        pending: dict[int, dict] = {}
        entered: set[int] = set()
        action_keys: set[str] = set()
        action_audit: list[dict] = []
        trades: list[dict] = []
        ignored_future_versions = sum(
            1 for values in filtered.values() for item in values if item.available_at > end_at
        )
        max_members = max(1, len(target_ids))

        for event_at in event_times:
            for action in actions:
                if action.action_key in action_keys or action.effective_at > event_at:
                    continue
                if action.available_at > event_at:
                    continue
                action_keys.add(action.action_key)
                quantity = positions[action.instrument_id]
                if quantity <= 0:
                    continue
                if action.action_type == "split":
                    positions[action.instrument_id] = quantity * action.value
                    average_cost[action.instrument_id] = average_cost[action.instrument_id] / action.value
                else:
                    if action.currency and action.currency != currency:
                        raise BacktestError(
                            "公司行动币种不匹配", error_code="corporate_action_currency_mismatch"
                        )
                    cash += quantity * action.value
                action_audit.append(
                    {
                        "action_key": action.action_key, "instrument_id": action.instrument_id,
                        "action_type": action.action_type, "effective_at": _utc_text(action.effective_at),
                        "available_at": _utc_text(action.available_at), "value": _money(action.value),
                    }
                )

            for instrument in sorted(list(pending)):
                if not active(instrument, event_at):
                    pending.pop(instrument, None)
                    continue
                bar = self._bar_at(filtered.get(instrument, ()), event_at)
                if bar is None or bar.market_status in BLOCKED_MARKET_STATES:
                    continue
                order = pending.pop(instrument)
                side = str(order["side"])
                execution_price = bar.open * (
                    Decimal("1") + slip if side == "buy" else Decimal("1") - slip
                )
                if side == "buy":
                    allocation = capital / Decimal(max_members)
                    spendable = min(cash, allocation)
                    quantity = (spendable / (execution_price * (Decimal("1") + fee))).to_integral_value(
                        rounding="ROUND_FLOOR"
                    )
                    if instruments[instrument]["asset_type"] == "fund":
                        quantity = (spendable / (execution_price * (Decimal("1") + fee))).quantize(
                            Decimal("0.0001"), rounding="ROUND_FLOOR"
                        )
                    if quantity <= 0:
                        continue
                    notional = quantity * execution_price
                    charge = notional * fee
                    cash -= notional + charge
                    previous = positions[instrument]
                    positions[instrument] += quantity
                    average_cost[instrument] = (
                        previous * average_cost[instrument] + notional + charge
                    ) / positions[instrument]
                else:
                    quantity = positions[instrument]
                    if quantity <= 0:
                        continue
                    notional = quantity * execution_price
                    charge = notional * fee
                    cash += notional - charge
                    positions[instrument] = Decimal("0")
                    average_cost[instrument] = Decimal("0")
                trades.append(
                    {
                        "instrument_id": instrument, "side": side,
                        "quantity": _money(quantity), "price": _money(execution_price),
                        "fee": _money(charge), "signal_at": _utc_text(order["signal_at"]),
                        "executed_at": _utc_text(event_at),
                        "reason": {
                            "strategy_key": strategy, "strategy_version": version,
                            "signal": order["reason"], "execution_lag_bars": 1,
                            "price_field": "open", "snapshot_id": bar.snapshot_id,
                            "snapshot_sha256": bar.snapshot_sha256,
                            "known_data_through": _utc_text(order["signal_at"]),
                            "available_at": _utc_text(bar.available_at),
                            "execution_mode": "paper", "real_order_execution": False,
                        },
                    }
                )

            for instrument in sorted(target_ids):
                if instrument in pending or not active(instrument, event_at):
                    continue
                current = self._bar_at(filtered.get(instrument, ()), event_at)
                if current is None or current.market_status in BLOCKED_MARKET_STATES:
                    continue
                known = self._known_bars_as_of(filtered.get(instrument, ()), event_at)
                if not known or known[-1].observed_at != event_at:
                    continue
                signal = ""
                side = ""
                if strategy == "buy_and_hold_v1":
                    if instrument not in entered and positions[instrument] <= 0:
                        signal, side = "first_point_in_time_observation", "buy"
                        entered.add(instrument)
                else:
                    closes = [item.close for item in known]
                    if len(closes) >= long_window:
                        short_average = sum(closes[-short_window:]) / Decimal(short_window)
                        long_average = sum(closes[-long_window:]) / Decimal(long_window)
                        if positions[instrument] <= 0 and short_average > long_average:
                            signal, side = "short_sma_above_long_sma", "buy"
                        elif positions[instrument] > 0 and short_average <= long_average:
                            signal, side = "short_sma_not_above_long_sma", "sell"
                if side:
                    pending[instrument] = {
                        "side": side, "signal_at": event_at, "reason": signal,
                    }

            for instrument in sorted(target_ids):
                if last_active_event.get(instrument) != event_at:
                    continue
                pending.pop(instrument, None)
                quantity = positions[instrument]
                if quantity <= 0:
                    continue
                bar = self._bar_at(filtered.get(instrument, ()), event_at)
                if bar is None or bar.market_status in BLOCKED_MARKET_STATES:
                    continue
                execution_price = bar.close * (Decimal("1") - slip)
                notional = quantity * execution_price
                charge = notional * fee
                cash += notional - charge
                positions[instrument] = Decimal("0")
                trades.append(
                    {
                        "instrument_id": instrument, "side": "sell",
                        "quantity": _money(quantity), "price": _money(execution_price),
                        "fee": _money(charge), "signal_at": _utc_text(event_at),
                        "executed_at": _utc_text(event_at),
                        "reason": {
                            "strategy_key": strategy, "strategy_version": version,
                            "signal": "forced_end_of_period_liquidation",
                            "price_field": "close", "snapshot_id": bar.snapshot_id,
                            "snapshot_sha256": bar.snapshot_sha256,
                            "known_data_through": _utc_text(event_at),
                            "available_at": _utc_text(bar.available_at),
                            "execution_mode": "paper", "real_order_execution": False,
                        },
                    }
                )

        business_days = sum(
            1 for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).weekday() < 5
        )
        usable_by_instrument = {
            instrument: len({
                item.observed_at for item in values
                if item.available_at <= item.observed_at
            })
            for instrument, values in filtered.items()
        }
        expected = max(1, business_days * max(1, len(target_ids)))
        usable = sum(usable_by_instrument.values())
        missing_instruments = sorted(
            target_ids - {instrument for instrument, values in filtered.items() if values}
        )
        unknown_member_versions = 0
        if universe:
            unknown_member_versions = sum(
                1 for item in member_manifest if not str(item.get("source_observed_at") or "")
            )
        limitations = []
        if missing_instruments:
            limitations.append("scope_members_without_persisted_history")
        if usable < expected:
            limitations.append("historical_calendar_coverage_below_full")
        if any(instruments[item]["asset_type"] == "fund" for item in target_ids):
            limitations.append("fund_nav_uses_disclosed_observations_without_interpolation")
        if unknown_member_versions:
            limitations.append("constituents_without_observed_time_excluded")
        coverage = {
            "expected_business_day_observations": expected,
            "usable_point_in_time_observations": usable,
            "coverage_ratio": round(min(1.0, usable / expected), 8),
            "usable_by_instrument": {str(key): value for key, value in usable_by_instrument.items()},
            "missing_instrument_ids": missing_instruments,
            "future_or_late_versions_ignored": ignored_future_versions,
            "unknown_member_versions": unknown_member_versions,
            "limitations": sorted(set(limitations)),
        }
        data_version = _sha(
            {
                "snapshots": manifest, "benchmark": benchmark_manifest,
                "universe_members": member_manifest, "data_cutoff_at": _utc_text(cutoff),
            }
        )
        final_config = {
            **request_contract,
            "backtest_version": FINANCIAL_BACKTEST_VERSION,
            "request_fingerprint": fingerprint, "data_version": data_version,
            "snapshot_manifest": manifest, "benchmark_manifest": benchmark_manifest,
            "universe_manifest": universe, "coverage": coverage,
            "corporate_action_audit": action_audit,
            "point_in_time_policy": {
                "bar_rule": "observed_at<=signal_at_and_available_at<=signal_at",
                "signal_execution": "next_tradable_observation_open",
                "constituent_rule": "effective_range_and_source_observed_at<=signal_at",
                "fund_nav_interpolation": False, "implicit_fx": False,
                "execution_mode": "paper", "real_order_execution": False,
            },
            "ending_cash": _money(cash),
        }
        now_text = _utc_text(self.clock().astimezone(timezone.utc))
        with self.lock:
            self.connection.execute("SAVEPOINT point_in_time_backtest")
            try:
                self.connection.execute(
                    """
                    INSERT INTO backtest_runs(
                        id, strategy_key, model_version, scope_type,
                        instrument_id, universe_id, start_date, end_date,
                        initial_capital, config_json, status, data_cutoff_at,
                        started_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        run_id, strategy, version, scope, instrument_id, universe_id,
                        start.isoformat(), end.isoformat(), _money(capital),
                        _canonical_json({
                            "owner_user_id": owner, "request_fingerprint": fingerprint,
                            "execution_mode": "paper", "real_order_execution": False,
                        }),
                        _utc_text(cutoff), now_text,
                    ),
                )
                for item in trades:
                    self.connection.execute(
                        """
                        INSERT INTO backtest_trades(
                            backtest_run_id, instrument_id, side, quantity,
                            price, fee, signal_at, executed_at, reason_json
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id, item["instrument_id"], item["side"], item["quantity"],
                            item["price"], item["fee"], item["signal_at"], item["executed_at"],
                            _canonical_json(item["reason"]),
                        ),
                    )
                self.connection.execute(
                    """
                    UPDATE backtest_runs SET config_json=?, status='completed',
                        completed_at=? WHERE id=?
                    """,
                    (_canonical_json(final_config), now_text, run_id),
                )
                self.connection.execute("RELEASE SAVEPOINT point_in_time_backtest")
            except Exception:
                self.connection.execute("ROLLBACK TO SAVEPOINT point_in_time_backtest")
                self.connection.execute("RELEASE SAVEPOINT point_in_time_backtest")
                raise
        result = self._existing(run_id, owner, fingerprint)
        if result is None:
            raise RuntimeError("completed backtest could not be reloaded")
        result["idempotent"] = False
        return result


__all__ = [
    "BacktestError", "FINANCIAL_BACKTEST_VERSION",
    "FinancialPointInTimeBacktester", "SUPPORTED_STRATEGIES",
]
