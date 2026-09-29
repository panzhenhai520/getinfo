#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Transactional paper account and order ledger over the existing SQLite DB."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from financial_config import require_financial_product_capability
from financial_backtest import FinancialPointInTimeBacktester
from financial_backtest_metrics import FinancialBacktestAnalytics
from financial_report_view import TERMINAL_REPORT_DENYLIST


FINANCIAL_PAPER_LEDGER_VERSION = "financial-paper-ledger-v1"
ORDER_TYPES = frozenset({"market", "limit", "stop"})
ORDER_STATUSES = frozenset({"pending", "partial", "completed", "cancelled", "rejected"})
OPEN_MARKET_STATES = frozenset({"open", "continuous", "trading", "auction"})
BLOCKED_MARKET_STATES = frozenset({"suspended", "halted", "delisted"})


class PaperTradingError(ValueError):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)
        self.retryable = False


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise PaperTradingError(f"{name} 无效", error_code=f"invalid_{name}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaperTradingError(f"{name} 无效", error_code=f"invalid_{name}") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise PaperTradingError(f"{name} 无效", error_code=f"invalid_{name}")
    return result


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.00000001")))


def _json_object(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x1f".join(str(item or "") for item in parts).encode("utf-8")
    ).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _price_from_payload(payload: Mapping[str, object]) -> Decimal:
    normalized = _json_object(payload.get("normalized_payload"))
    for key in ("last_price", "price", "close", "nav", "index_level", "value"):
        value = payload.get(key, normalized.get(key))
        if isinstance(value, Mapping):
            value = value.get("number")
        if value not in (None, ""):
            return _decimal(value, "snapshot_price", positive=True)
    raise PaperTradingError(
        "执行快照缺少可用价格", error_code="snapshot_price_missing"
    )


class FinancialPaperLedger:
    """Paper-only mutations with atomic cash, fill and position updates."""

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
            "simulation", str(pack_id or ""), settings=self.settings
        )

    def _account(self, account_id: str, owner_user_id: object) -> dict:
        row = self.connection.execute(
            """
            SELECT id, account_name, base_currency, initial_cash, cash_balance,
                   status, execution_mode, config_json, created_at, updated_at
            FROM paper_accounts WHERE id=?
            """,
            (str(account_id or ""),),
        ).fetchone()
        if row is None:
            raise PaperTradingError("纸面账户不存在", error_code="paper_account_not_found")
        config_value = _json_object(row[7])
        if str(config_value.get("owner_user_id") or "") != str(owner_user_id or ""):
            raise PermissionError("无权访问该纸面账户")
        return {
            "account_id": str(row[0]),
            "account_name": str(row[1]),
            "base_currency": str(row[2]),
            "initial_cash": float(row[3]),
            "cash_balance": float(row[4]),
            "status": str(row[5]),
            "execution_mode": str(row[6]),
            "config": config_value,
            "created_at": str(row[8]),
            "updated_at": str(row[9]),
        }

    def create_account(
        self,
        *,
        account_name: str,
        base_currency: str,
        initial_cash: object,
        owner_user_id: object,
        industry_pack_id: str,
        idempotency_key: str = "",
    ) -> dict:
        self._require(industry_pack_id)
        owner = str(owner_user_id or "").strip()
        if not owner:
            raise PermissionError("纸面账户必须关联登录用户")
        name = str(account_name or "").strip()[:120]
        currency = str(base_currency or "").strip().upper()
        cash = _decimal(initial_cash, "initial_cash", positive=True)
        if not name:
            raise PaperTradingError("账户名称不能为空", error_code="account_name_required")
        if not currency.isalpha() or len(currency) != 3:
            raise PaperTradingError("基础币种无效", error_code="invalid_base_currency")
        key = str(idempotency_key or uuid.uuid4().hex).strip()
        if len(key) > 160:
            raise PaperTradingError("幂等键过长", error_code="invalid_idempotency_key")
        account_id = _stable_id("paper-account", owner, key)
        now = _utc_text(self.clock())
        config_value = {
            "ledger_version": FINANCIAL_PAPER_LEDGER_VERSION,
            "owner_user_id": owner,
            "industry_pack_id": str(industry_pack_id),
            "idempotency_key": key,
            "real_order_execution": False,
        }
        with self.lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO paper_accounts(
                    id, account_name, base_currency, initial_cash, cash_balance,
                    status, execution_mode, config_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'active', 'paper', ?, ?, ?)
                """,
                (
                    account_id, name, currency, _money(cash), _money(cash),
                    json.dumps(config_value, ensure_ascii=False, sort_keys=True), now, now,
                ),
            )
            created = cursor.rowcount == 1
        account = self._account(account_id, owner)
        return {**account, "created": created, "real_order_execution": False}

    def _instrument(self, instrument_id: int) -> dict:
        row = self.connection.execute(
            """
            SELECT id, canonical_symbol, display_name, asset_type, market,
                   exchange, currency, listing_status
            FROM financial_instruments WHERE id=?
            """,
            (int(instrument_id),),
        ).fetchone()
        if row is None:
            raise PaperTradingError("标的不存在", error_code="instrument_not_found")
        if str(row[7]).casefold() != "active":
            raise PaperTradingError("标的不可交易", error_code="instrument_not_active")
        return {
            "instrument_id": int(row[0]), "canonical_symbol": str(row[1]),
            "display_name": str(row[2]), "asset_type": str(row[3]),
            "market": str(row[4]), "exchange": str(row[5]),
            "currency": str(row[6]).upper(),
        }

    def _report_contract(self, report_id: int, instrument_id: int) -> dict:
        row = self.connection.execute(
            """
            SELECT report.id, report.report_version, report.report_status,
                   report.research_run_id, run.instrument_id
            FROM financial_final_reports report
            JOIN financial_research_runs run ON run.id=report.research_run_id
            WHERE report.id=?
            """,
            (int(report_id),),
        ).fetchone()
        if row is None or str(row[2]).casefold() in TERMINAL_REPORT_DENYLIST:
            raise PaperTradingError(
                "订单必须关联已保存终态报告", error_code="terminal_report_required"
            )
        if row[4] is None or int(row[4]) != int(instrument_id):
            raise PaperTradingError(
                "报告标的与订单不一致", error_code="report_instrument_mismatch"
            )
        return {
            "final_report_id": int(row[0]),
            "report_version": int(row[1]),
            "research_run_id": str(row[3]),
        }

    def _snapshot(self, snapshot_id: int, instrument_id: int) -> dict:
        row = self.connection.execute(
            """
            SELECT id, instrument_id, data_type, observed_at, fetched_at,
                   market_status, currency, stale_after, quality_status,
                   payload_json, payload_sha256, source_url
            FROM financial_data_snapshots WHERE id=?
            """,
            (int(snapshot_id),),
        ).fetchone()
        if row is None:
            raise PaperTradingError("快照不存在", error_code="snapshot_not_found")
        if row[1] is None or int(row[1]) != int(instrument_id):
            raise PaperTradingError(
                "快照标的与订单不一致", error_code="snapshot_instrument_mismatch"
            )
        payload = _json_object(row[9])
        payload_text = str(row[9] or "")
        integrity_valid = (
            hashlib.sha256(payload_text.encode("utf-8")).hexdigest() == str(row[10])
        )
        if not integrity_valid:
            raise PaperTradingError(
                "快照 hash 校验失败", error_code="snapshot_integrity_failed"
            )
        return {
            "snapshot_id": int(row[0]), "instrument_id": int(row[1]),
            "data_type": str(row[2]), "observed_at": str(row[3]),
            "fetched_at": str(row[4]), "market_status": str(row[5]).casefold(),
            "currency": str(row[6]).upper(), "stale_after": str(row[7] or ""),
            "quality_status": str(row[8]).casefold(), "payload": payload,
            "payload_sha256": str(row[10]), "source_url": str(row[11]),
            "integrity_valid": integrity_valid,
        }

    def submit_order(
        self,
        *,
        account_id: str,
        instrument_id: int,
        side: str,
        order_type: str,
        quantity: object,
        final_report_id: int,
        strategy_version: str,
        signal_snapshot_id: int,
        owner_user_id: object,
        idempotency_key: str,
        limit_price: object = None,
        stop_price: object = None,
        research_instrument_id: int | None = None,
        index_proxy_confirmed: bool = False,
    ) -> dict:
        account = self._account(account_id, owner_user_id)
        self._require(str(account["config"].get("industry_pack_id") or ""))
        if account["status"] != "active" or account["execution_mode"] != "paper":
            raise PaperTradingError("纸面账户不可用", error_code="paper_account_inactive")
        instrument = self._instrument(int(instrument_id))
        research_instrument = self._instrument(
            int(research_instrument_id or instrument_id)
        )
        if instrument["asset_type"].casefold() == "index":
            raise PaperTradingError(
                "指数不可直接下单，必须选择可交易代理",
                error_code="index_requires_tradable_proxy",
            )
        if research_instrument["asset_type"].casefold() == "index":
            if int(research_instrument["instrument_id"]) == int(instrument_id) or not index_proxy_confirmed:
                raise PaperTradingError(
                    "指数研究必须由用户确认可交易代理",
                    error_code="index_proxy_confirmation_required",
                )
        elif int(research_instrument["instrument_id"]) != int(instrument_id):
            raise PaperTradingError(
                "非指数报告不能映射到其他交易标的",
                error_code="research_instrument_mismatch",
            )
        if instrument["currency"] and instrument["currency"] != account["base_currency"]:
            raise PaperTradingError(
                "纸面账户不执行隐式外汇换算", error_code="currency_mismatch"
            )
        normalized_side = str(side or "").casefold()
        normalized_type = str(order_type or "").casefold()
        qty = _decimal(quantity, "quantity", positive=True)
        if normalized_side not in {"buy", "sell"}:
            raise PaperTradingError("订单方向无效", error_code="invalid_side")
        if normalized_type not in ORDER_TYPES:
            raise PaperTradingError("订单类型无效", error_code="invalid_order_type")
        limit_value = (
            _decimal(limit_price, "limit_price", positive=True)
            if limit_price not in (None, "") else None
        )
        stop_value = (
            _decimal(stop_price, "stop_price", positive=True)
            if stop_price not in (None, "") else None
        )
        if normalized_type == "limit" and limit_value is None:
            raise PaperTradingError("限价单缺少限价", error_code="limit_price_required")
        if normalized_type == "stop" and stop_value is None:
            raise PaperTradingError("止损单缺少触发价", error_code="stop_price_required")
        strategy = str(strategy_version or "").strip()[:160]
        if not strategy:
            raise PaperTradingError("策略版本不能为空", error_code="strategy_version_required")
        report = self._report_contract(
            int(final_report_id), int(research_instrument["instrument_id"])
        )
        signal = self._snapshot(
            int(signal_snapshot_id), int(research_instrument["instrument_id"])
        )
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 160:
            raise PaperTradingError("订单幂等键无效", error_code="invalid_idempotency_key")
        order_id = _stable_id("paper-order", account_id, key)
        metadata = {
            "ledger_version": FINANCIAL_PAPER_LEDGER_VERSION,
            "idempotency_key": key,
            "strategy_version": strategy,
            "report_version": report["report_version"],
            "signal_snapshot_id": signal["snapshot_id"],
            "signal_snapshot_sha256": signal["payload_sha256"],
            "research_instrument_id": int(research_instrument["instrument_id"]),
            "tradable_proxy_instrument_id": int(instrument_id),
            "index_proxy_confirmed": bool(index_proxy_confirmed),
            "real_order_execution": False,
        }
        with self.lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO paper_orders(
                    id, account_id, instrument_id, research_run_id,
                    final_report_id, side, order_type, quantity, limit_price,
                    stop_price, status, metadata_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    order_id, account_id, int(instrument_id), report["research_run_id"],
                    report["final_report_id"], normalized_side, normalized_type,
                    _money(qty), _money(limit_value) if limit_value is not None else None,
                    _money(stop_value) if stop_value is not None else None,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            created = cursor.rowcount == 1
        order = self.get_order(order_id, owner_user_id=owner_user_id)
        return {**order, "created": created}

    def get_order(self, order_id: str, *, owner_user_id: object) -> dict:
        row = self.connection.execute(
            """
            SELECT id, account_id, instrument_id, research_run_id,
                   final_report_id, side, order_type, quantity, limit_price,
                   stop_price, status, submitted_at, completed_at, cancelled_at,
                   metadata_json
            FROM paper_orders WHERE id=?
            """,
            (str(order_id or ""),),
        ).fetchone()
        if row is None:
            raise PaperTradingError("纸面订单不存在", error_code="paper_order_not_found")
        self._account(str(row[1]), owner_user_id)
        filled = self.connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM paper_fills WHERE order_id=?",
            (str(row[0]),),
        ).fetchone()[0]
        return {
            "order_id": str(row[0]), "account_id": str(row[1]),
            "instrument_id": int(row[2]), "research_run_id": str(row[3] or ""),
            "final_report_id": int(row[4]) if row[4] is not None else None,
            "side": str(row[5]), "order_type": str(row[6]),
            "quantity": float(row[7]), "filled_quantity": float(filled or 0),
            "remaining_quantity": max(0.0, float(row[7]) - float(filled or 0)),
            "limit_price": float(row[8]) if row[8] is not None else None,
            "stop_price": float(row[9]) if row[9] is not None else None,
            "status": str(row[10]), "submitted_at": str(row[11]),
            "completed_at": str(row[12] or ""), "cancelled_at": str(row[13] or ""),
            "metadata": _json_object(row[14]), "execution_mode": "paper",
            "real_order_execution": False,
        }

    def _position(self, account_id: str, instrument_id: int) -> dict:
        row = self.connection.execute(
            """
            SELECT quantity, average_cost, realized_pnl, last_price,
                   market_value, as_of
            FROM paper_positions WHERE account_id=? AND instrument_id=?
            """,
            (account_id, int(instrument_id)),
        ).fetchone()
        if row is None:
            return {
                "quantity": Decimal("0"), "average_cost": Decimal("0"),
                "realized_pnl": Decimal("0"), "last_price": None,
                "market_value": Decimal("0"), "as_of": "",
            }
        return {
            "quantity": _decimal(row[0], "position_quantity"),
            "average_cost": _decimal(row[1], "average_cost"),
            "realized_pnl": _decimal(row[2], "realized_pnl"),
            "last_price": _decimal(row[3], "last_price") if row[3] is not None else None,
            "market_value": _decimal(row[4], "market_value") if row[4] is not None else Decimal("0"),
            "as_of": str(row[5]),
        }

    @staticmethod
    def _trigger_reason(order: Mapping[str, object], market_price: Decimal, payload: Mapping[str, object]) -> str:
        side = str(order["side"])
        kind = str(order["order_type"])
        if kind == "limit":
            limit_price = _decimal(order["limit_price"], "limit_price", positive=True)
            if side == "buy" and market_price > limit_price:
                return "limit_not_reached"
            if side == "sell" and market_price < limit_price:
                return "limit_not_reached"
        if kind == "stop":
            stop_price = _decimal(order["stop_price"], "stop_price", positive=True)
            if side == "buy" and market_price < stop_price:
                return "stop_not_triggered"
            if side == "sell" and market_price > stop_price:
                return "stop_not_triggered"
        limit_up = payload.get("price_limit_up")
        limit_down = payload.get("price_limit_down")
        if side == "buy" and limit_up not in (None, ""):
            if market_price >= _decimal(limit_up, "price_limit_up", positive=True):
                return "buy_blocked_at_price_limit_up"
        if side == "sell" and limit_down not in (None, ""):
            if market_price <= _decimal(limit_down, "price_limit_down", positive=True):
                return "sell_blocked_at_price_limit_down"
        return ""

    def fill_order(
        self,
        order_id: str,
        *,
        execution_snapshot_id: int,
        owner_user_id: object,
        idempotency_key: str,
        quantity: object = None,
        fee_rate: object = "0.001",
        slippage_bps: object = "0",
        filled_at: object = None,
    ) -> dict:
        order = self.get_order(order_id, owner_user_id=owner_user_id)
        account = self._account(order["account_id"], owner_user_id)
        self._require(str(account["config"].get("industry_pack_id") or ""))
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 160:
            raise PaperTradingError("成交幂等键无效", error_code="invalid_idempotency_key")
        fill_id = _stable_id("paper-fill", order_id, key)
        existing = self.connection.execute(
            "SELECT quantity, price, fee, snapshot_id, filled_at FROM paper_fills WHERE id=?",
            (fill_id,),
        ).fetchone()
        if existing is not None:
            return {
                "status": order["status"], "order_id": order_id, "fill_id": fill_id,
                "quantity": float(existing[0]), "price": float(existing[1]),
                "fee": float(existing[2]), "snapshot_id": int(existing[3]),
                "filled_at": str(existing[4]), "idempotent": True,
                "execution_mode": "paper", "real_order_execution": False,
            }
        if order["status"] not in {"pending", "partial"}:
            raise PaperTradingError("订单不可成交", error_code="order_not_fillable")
        remaining = _decimal(order["remaining_quantity"], "remaining_quantity", positive=True)
        fill_qty = remaining if quantity in (None, "") else _decimal(quantity, "fill_quantity", positive=True)
        if fill_qty > remaining:
            raise PaperTradingError("成交数量超过剩余数量", error_code="fill_exceeds_remaining")
        snapshot = self._snapshot(int(execution_snapshot_id), int(order["instrument_id"]))
        fill_time = _utc(filled_at) if filled_at not in (None, "") else self.clock()
        if fill_time is None:
            raise PaperTradingError("成交时间必须含时区", error_code="invalid_filled_at")
        observed = _utc(snapshot["observed_at"])
        stale_after = _utc(snapshot["stale_after"])
        if observed is None or observed > fill_time:
            raise PaperTradingError("不能用未来快照成交", error_code="future_snapshot")
        if stale_after is not None and stale_after <= fill_time:
            raise PaperTradingError("不能用过期快照成交", error_code="stale_snapshot")
        if snapshot["quality_status"] not in {"verified", "normalized", "available"}:
            raise PaperTradingError("快照未经核验", error_code="snapshot_not_verified")
        market_status = snapshot["market_status"]
        if market_status in BLOCKED_MARKET_STATES:
            raise PaperTradingError("标的停牌或不可交易", error_code="instrument_suspended")
        if market_status not in OPEN_MARKET_STATES:
            raise PaperTradingError("市场当前不可成交", error_code="market_not_open")
        if snapshot["currency"] and snapshot["currency"] != account["base_currency"]:
            raise PaperTradingError("成交币种不匹配", error_code="currency_mismatch")
        market_price = _price_from_payload(snapshot["payload"])
        trigger_reason = self._trigger_reason(order, market_price, snapshot["payload"])
        if trigger_reason:
            return {
                "status": order["status"], "order_id": order_id, "filled": False,
                "reason": trigger_reason, "execution_mode": "paper",
                "real_order_execution": False,
            }
        fee_fraction = _decimal(fee_rate, "fee_rate")
        slip = _decimal(slippage_bps, "slippage_bps") / Decimal("10000")
        if fee_fraction < 0 or slip < 0 or slip > Decimal("0.1"):
            raise PaperTradingError("费用或滑点参数无效", error_code="invalid_execution_cost")
        execution_price = market_price * (
            Decimal("1") + slip if order["side"] == "buy" else Decimal("1") - slip
        )
        if order["order_type"] == "limit":
            limit_value = _decimal(order["limit_price"], "limit_price", positive=True)
            if (order["side"] == "buy" and execution_price > limit_value) or (
                order["side"] == "sell" and execution_price < limit_value
            ):
                return {
                    "status": order["status"], "order_id": order_id, "filled": False,
                    "reason": "slippage_exceeds_limit", "execution_mode": "paper",
                    "real_order_execution": False,
                }
        notional = fill_qty * execution_price
        fee = notional * fee_fraction
        fill_time_text = _utc_text(fill_time)
        with self.lock:
            self.connection.execute("SAVEPOINT paper_fill_atomic")
            try:
                persisted = self.connection.execute(
                    """
                    SELECT orders.quantity, orders.status,
                           COALESCE(SUM(fills.quantity), 0)
                    FROM paper_orders orders
                    LEFT JOIN paper_fills fills ON fills.order_id=orders.id
                    WHERE orders.id=? GROUP BY orders.id
                    """,
                    (order_id,),
                ).fetchone()
                if persisted is None or str(persisted[1]) not in {"pending", "partial"}:
                    raise PaperTradingError("订单不可成交", error_code="order_not_fillable")
                current_remaining = _decimal(persisted[0], "quantity", positive=True) - _decimal(
                    persisted[2], "filled_quantity"
                )
                if fill_qty > current_remaining:
                    raise PaperTradingError(
                        "成交数量超过剩余数量", error_code="fill_exceeds_remaining"
                    )
                duplicate = self.connection.execute(
                    "SELECT id FROM paper_fills WHERE id=?", (fill_id,)
                ).fetchone()
                if duplicate is not None:
                    raise PaperTradingError(
                        "成交幂等记录已存在", error_code="duplicate_fill_race"
                    )
                refreshed_account = self._account(order["account_id"], owner_user_id)
                position = self._position(order["account_id"], int(order["instrument_id"]))
                cash = _decimal(refreshed_account["cash_balance"], "cash_balance")
                if order["side"] == "buy":
                    required = notional + fee
                    if cash < required:
                        raise PaperTradingError("纸面账户资金不足", error_code="insufficient_cash")
                    new_cash = cash - required
                    new_quantity = position["quantity"] + fill_qty
                    new_average = (
                        position["quantity"] * position["average_cost"] + notional + fee
                    ) / new_quantity
                    realized = position["realized_pnl"]
                else:
                    if position["quantity"] < fill_qty:
                        raise PaperTradingError("纸面持仓不足", error_code="insufficient_position")
                    new_cash = cash + notional - fee
                    new_quantity = position["quantity"] - fill_qty
                    new_average = position["average_cost"] if new_quantity > 0 else Decimal("0")
                    realized = position["realized_pnl"] + (
                        execution_price - position["average_cost"]
                    ) * fill_qty - fee
                self.connection.execute(
                    """
                    INSERT INTO paper_fills(
                        id, order_id, quantity, price, fee, currency,
                        snapshot_id, filled_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill_id, order_id, _money(fill_qty), _money(execution_price),
                        _money(fee), account["base_currency"], snapshot["snapshot_id"],
                        fill_time_text,
                    ),
                )
                total_filled = _decimal(persisted[2], "filled_quantity") + fill_qty
                new_status = "completed" if total_filled == _decimal(persisted[0], "quantity") else "partial"
                self.connection.execute(
                    """
                    UPDATE paper_orders SET status=?, completed_at=?
                    WHERE id=? AND status IN ('pending','partial')
                    """,
                    (new_status, fill_time_text if new_status == "completed" else None, order_id),
                )
                self.connection.execute(
                    """
                    UPDATE paper_accounts SET cash_balance=?,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id=?
                    """,
                    (_money(new_cash), order["account_id"]),
                )
                self.connection.execute(
                    """
                    INSERT INTO paper_positions(
                        account_id, instrument_id, quantity, average_cost,
                        realized_pnl, last_price, market_value, as_of
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, instrument_id) DO UPDATE SET
                        quantity=excluded.quantity,
                        average_cost=excluded.average_cost,
                        realized_pnl=excluded.realized_pnl,
                        last_price=excluded.last_price,
                        market_value=excluded.market_value,
                        as_of=excluded.as_of,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (
                        order["account_id"], int(order["instrument_id"]),
                        _money(new_quantity), _money(new_average), _money(realized),
                        _money(market_price), _money(new_quantity * market_price),
                        snapshot["observed_at"],
                    ),
                )
                self.connection.execute("RELEASE SAVEPOINT paper_fill_atomic")
            except Exception:
                self.connection.execute("ROLLBACK TO SAVEPOINT paper_fill_atomic")
                self.connection.execute("RELEASE SAVEPOINT paper_fill_atomic")
                raise
        return {
            "status": new_status, "order_id": order_id, "fill_id": fill_id,
            "filled": True, "quantity": _money(fill_qty),
            "price": _money(execution_price), "fee": _money(fee),
            "snapshot_id": snapshot["snapshot_id"], "filled_at": fill_time_text,
            "cash_balance": _money(new_cash), "position_quantity": _money(new_quantity),
            "idempotent": False, "execution_mode": "paper",
            "real_order_execution": False,
        }

    def cancel_order(
        self, order_id: str, *, owner_user_id: object
    ) -> dict:
        order = self.get_order(order_id, owner_user_id=owner_user_id)
        account = self._account(order["account_id"], owner_user_id)
        self._require(str(account["config"].get("industry_pack_id") or ""))
        if order["status"] == "cancelled":
            return {**order, "idempotent": True}
        if order["status"] not in {"pending", "partial"}:
            raise PaperTradingError("订单不可撤销", error_code="order_not_cancellable")
        now = _utc_text(self.clock())
        with self.lock:
            self.connection.execute(
                """
                UPDATE paper_orders SET status='cancelled', cancelled_at=?
                WHERE id=? AND status IN ('pending','partial')
                """,
                (now, order_id),
            )
        return {**self.get_order(order_id, owner_user_id=owner_user_id), "idempotent": False}

    def account_statement(self, account_id: str, *, owner_user_id: object) -> dict:
        account = self._account(account_id, owner_user_id)
        positions = self.connection.execute(
            """
            SELECT position.instrument_id, instrument.canonical_symbol,
                   instrument.display_name, position.quantity,
                   position.average_cost, position.realized_pnl,
                   position.last_price, position.market_value, position.as_of
            FROM paper_positions position
            JOIN financial_instruments instrument ON instrument.id=position.instrument_id
            WHERE position.account_id=? ORDER BY instrument.canonical_symbol
            """,
            (account_id,),
        ).fetchall()
        flow = self.connection.execute(
            """
            SELECT orders.side,
                   COALESCE(SUM(fills.quantity * fills.price), 0),
                   COALESCE(SUM(fills.fee), 0), COUNT(fills.id)
            FROM paper_orders orders
            JOIN paper_fills fills ON fills.order_id=orders.id
            WHERE orders.account_id=? GROUP BY orders.side
            """,
            (account_id,),
        ).fetchall()
        bought = sold = fees = Decimal("0")
        fill_count = 0
        for side, notional, fee, count in flow:
            if str(side) == "buy":
                bought += _decimal(notional, "buy_notional")
            else:
                sold += _decimal(notional, "sell_notional")
            fees += _decimal(fee, "fees")
            fill_count += int(count)
        expected_cash = _decimal(account["initial_cash"], "initial_cash") - bought + sold - fees
        actual_cash = _decimal(account["cash_balance"], "cash_balance")
        position_items = [
            {
                "instrument_id": int(row[0]), "canonical_symbol": str(row[1]),
                "display_name": str(row[2]), "quantity": float(row[3]),
                "average_cost": float(row[4]), "realized_pnl": float(row[5]),
                "last_price": float(row[6]) if row[6] is not None else None,
                "market_value": float(row[7]) if row[7] is not None else 0.0,
                "as_of": str(row[8]),
            }
            for row in positions
        ]
        market_value = sum(Decimal(str(item["market_value"])) for item in position_items)
        delta = actual_cash - expected_cash
        return {
            "ledger_version": FINANCIAL_PAPER_LEDGER_VERSION,
            "account": {key: value for key, value in account.items() if key != "config"},
            "positions": position_items,
            "fill_count": fill_count,
            "cash_flow": {
                "buy_notional": _money(bought), "sell_notional": _money(sold),
                "fees": _money(fees), "expected_cash": _money(expected_cash),
                "actual_cash": _money(actual_cash), "conservation_delta": _money(delta),
            },
            "equity": _money(actual_cash + market_value),
            "ledger_conserved": abs(delta) <= Decimal("0.000001"),
            "execution_mode": "paper",
            "real_order_execution": False,
        }


class FinancialPaperTradingJobService:
    """Runner injected into the existing ``paper_backtest`` worker handler."""

    def __init__(self, repository, *, settings=None):
        self.repository = repository
        self.ledger = FinancialPaperLedger(repository.db, settings=settings)
        self.backtester = FinancialPointInTimeBacktester(
            repository.db, settings=settings
        )
        self.backtest_analytics = FinancialBacktestAnalytics(
            repository.db, settings=settings
        )

    def runners(self) -> dict:
        return {"paper_backtest": self.run}

    def run(self, payload: Mapping[str, object], context) -> dict:
        context.raise_if_cancelled()
        task_kind = str(payload.get("task_kind") or "")
        parameters = _json_object(payload.get("parameters"))
        action = str(parameters.get("action") or "")
        owner = str(parameters.get("owner_user_id") or "")
        pack = str(payload.get("industry_pack_id") or "")
        if task_kind == "backtest":
            if action not in {"", "run"}:
                raise PaperTradingError(
                    "不支持的回测任务动作", error_code="unsupported_backtest_action"
                )
            backtest = self.backtester.run(
                owner_user_id=owner,
                industry_pack_id=pack,
                idempotency_key=str(
                    parameters.get("idempotency_key") or context.job_id
                ),
                strategy_key=str(parameters.get("strategy_key") or ""),
                strategy_version=str(parameters.get("strategy_version") or ""),
                scope_type=str(parameters.get("scope_type") or ""),
                instrument_id=parameters.get("instrument_id"),
                universe_id=parameters.get("universe_id"),
                start_date=parameters.get("start_date"),
                end_date=parameters.get("end_date"),
                initial_capital=parameters.get("initial_capital"),
                base_currency=str(parameters.get("base_currency") or ""),
                snapshot_ids=parameters.get("snapshot_ids") or (),
                data_cutoff_at=parameters.get("data_cutoff_at"),
                strategy_parameters=_json_object(parameters.get("strategy_parameters")),
                fee_rate=parameters.get("fee_rate", "0.001"),
                slippage_bps=parameters.get("slippage_bps", "0"),
                benchmark_snapshot_id=parameters.get("benchmark_snapshot_id"),
                random_seed=int(parameters.get("random_seed", 0)),
            )
            analytics = self.backtest_analytics.calculate(
                backtest["backtest_run_id"], owner_user_id=owner
            )
            return {**backtest, "analytics": analytics}
        if task_kind != "paper_trade":
            raise PaperTradingError(
                "不支持的模拟任务类型", error_code="unsupported_simulation_task"
            )
        if action == "create_account":
            return self.ledger.create_account(
                account_name=str(parameters.get("account_name") or "纸面账户"),
                base_currency=str(parameters.get("base_currency") or "CNY"),
                initial_cash=parameters.get("initial_cash"),
                owner_user_id=owner,
                industry_pack_id=pack,
                idempotency_key=str(parameters.get("idempotency_key") or context.job_id),
            )
        raise PaperTradingError(
            "不支持的纸面任务动作", error_code="unsupported_paper_action"
        )


__all__ = [
    "FINANCIAL_PAPER_LEDGER_VERSION",
    "FinancialPaperLedger",
    "FinancialPaperTradingJobService",
    "PaperTradingError",
]
