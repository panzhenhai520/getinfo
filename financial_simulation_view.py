#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Owner-scoped read projection for the paper trading/backtest Dashboard."""

from __future__ import annotations

import json
from collections.abc import Mapping

from financial_config import financial_product_capabilities


FINANCIAL_SIMULATION_VIEW_VERSION = "financial-simulation-view-v1"
DEFAULT_TRADE_LIMIT = 120
MAX_TRADE_LIMIT = 500
MAX_ACCOUNT_ITEMS = 100
MAX_BACKTEST_ITEMS = 100


def _json_object(value) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_list(value) -> list:
    if isinstance(value, list):
        return list(value)
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _report_id(config: Mapping[str, object]) -> int | None:
    parameters = config.get("strategy_parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    for value in (
        config.get("source_report_id"), config.get("final_report_id"),
        parameters.get("source_report_id"), parameters.get("final_report_id"),
    ):
        try:
            result = int(value)
        except (TypeError, ValueError):
            continue
        if result > 0:
            return result
    return None


class FinancialSimulationView:
    """Build a safe UI/export projection without enabling any execution path."""

    def __init__(self, database, *, settings=None):
        self.database = database
        self.settings = settings

    @property
    def connection(self):
        self.database._ensure_connection()
        return self.database.connection

    @staticmethod
    def _owner(config, owner_user_id: str) -> bool:
        return str(_json_object(config).get("owner_user_id") or "") == owner_user_id

    def _account(
        self, row, *, owner_user_id: str, history_limit: int | None = 100
    ) -> dict | None:
        config = _json_object(row[7])
        if str(config.get("owner_user_id") or "") != owner_user_id:
            return None
        account_id = str(row[0])
        positions = self.connection.execute(
            """
            SELECT position.instrument_id, instrument.canonical_symbol,
                   instrument.display_name, position.quantity,
                   position.average_cost, position.realized_pnl,
                   position.last_price, position.market_value, position.as_of
            FROM paper_positions position
            JOIN financial_instruments instrument ON instrument.id=position.instrument_id
            WHERE position.account_id=?
            ORDER BY instrument.canonical_symbol
            """,
            (account_id,),
        ).fetchall()
        order_limit = "" if history_limit is None else f" LIMIT {int(history_limit)}"
        fill_limit = "" if history_limit is None else f" LIMIT {int(history_limit) * 2}"
        order_total = int(self.connection.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE account_id=?", (account_id,)
        ).fetchone()[0])
        fill_total = int(self.connection.execute(
            """SELECT COUNT(*) FROM paper_fills fills
               JOIN paper_orders orders ON orders.id=fills.order_id
               WHERE orders.account_id=?""",
            (account_id,),
        ).fetchone()[0])
        orders = self.connection.execute(
            """
            SELECT orders.id, orders.instrument_id, instrument.canonical_symbol,
                   instrument.display_name, orders.research_run_id,
                   orders.final_report_id, orders.side, orders.order_type,
                   orders.quantity, orders.limit_price, orders.stop_price,
                   orders.status, orders.submitted_at, orders.completed_at,
                   orders.cancelled_at, orders.metadata_json,
                   COALESCE(SUM(fills.quantity),0), COALESCE(SUM(fills.fee),0),
                   COUNT(fills.id)
            FROM paper_orders orders
            JOIN financial_instruments instrument ON instrument.id=orders.instrument_id
            LEFT JOIN paper_fills fills ON fills.order_id=orders.id
            WHERE orders.account_id=?
            GROUP BY orders.id
            ORDER BY datetime(orders.submitted_at) DESC, orders.id DESC
            """ + order_limit,
            (account_id,),
        ).fetchall()
        fills = self.connection.execute(
            """
            SELECT fills.id, fills.order_id, fills.quantity, fills.price,
                   fills.fee, fills.currency, fills.snapshot_id, fills.filled_at,
                   orders.final_report_id, instrument.canonical_symbol,
                   instrument.display_name
            FROM paper_fills fills
            JOIN paper_orders orders ON orders.id=fills.order_id
            JOIN financial_instruments instrument ON instrument.id=orders.instrument_id
            WHERE orders.account_id=?
            ORDER BY datetime(fills.filled_at) DESC, fills.id DESC
            """ + fill_limit,
            (account_id,),
        ).fetchall()
        position_items = [
            {
                "instrument_id": int(item[0]), "canonical_symbol": str(item[1]),
                "display_name": str(item[2]), "quantity": float(item[3]),
                "average_cost": float(item[4]), "realized_pnl": float(item[5]),
                "last_price": float(item[6]) if item[6] is not None else None,
                "market_value": float(item[7] or 0), "as_of": str(item[8] or ""),
            }
            for item in positions
        ]
        order_items = []
        for item in orders:
            report_id = int(item[5]) if item[5] is not None else None
            quantity = float(item[8])
            filled_quantity = float(item[16] or 0)
            order_items.append(
                {
                    "order_id": str(item[0]), "instrument_id": int(item[1]),
                    "canonical_symbol": str(item[2]), "display_name": str(item[3]),
                    "research_run_id": str(item[4] or ""),
                    "final_report_id": report_id, "side": str(item[6]),
                    "order_type": str(item[7]), "quantity": quantity,
                    "filled_quantity": filled_quantity,
                    "remaining_quantity": max(0.0, quantity - filled_quantity),
                    "limit_price": float(item[9]) if item[9] is not None else None,
                    "stop_price": float(item[10]) if item[10] is not None else None,
                    "status": str(item[11]), "submitted_at": str(item[12] or ""),
                    "completed_at": str(item[13] or ""),
                    "cancelled_at": str(item[14] or ""),
                    "metadata": _json_object(item[15]),
                    "fee_total": float(item[17] or 0), "fill_count": int(item[18]),
                    "report_url": f"/api/financial/reports/{report_id}" if report_id else "",
                }
            )
        fill_items = [
            {
                "fill_id": str(item[0]), "order_id": str(item[1]),
                "quantity": float(item[2]), "price": float(item[3]),
                "fee": float(item[4]), "currency": str(item[5]),
                "snapshot_id": int(item[6]) if item[6] is not None else None,
                "filled_at": str(item[7]),
                "final_report_id": int(item[8]) if item[8] is not None else None,
                "canonical_symbol": str(item[9]), "display_name": str(item[10]),
                "evidence_url": f"/api/financial/snapshots/{int(item[6])}" if item[6] is not None else "",
            }
            for item in fills
        ]
        market_value = sum(float(item["market_value"] or 0) for item in position_items)
        return {
            "account_id": account_id, "account_name": str(row[1]),
            "base_currency": str(row[2]), "initial_cash": float(row[3]),
            "cash_balance": float(row[4]), "status": str(row[5]),
            "execution_mode": "paper", "real_order_execution": False,
            "created_at": str(row[8]), "updated_at": str(row[9]),
            "equity": float(row[4]) + market_value,
            "positions": position_items, "orders": order_items, "fills": fill_items,
            "position_count": len(position_items), "order_count": order_total,
            "fill_count": fill_total,
            "orders_truncated": order_total > len(order_items),
            "fills_truncated": fill_total > len(fill_items),
            "export_url": f"/api/intel/financial/simulation/export?kind=account&id={account_id}",
        }

    def _accounts(
        self, *, owner_user_id: str, account_id: str = "",
        history_limit: int | None = 100, item_limit: int | None = MAX_ACCOUNT_ITEMS,
    ) -> list[dict]:
        where = [
            "json_valid(config_json)",
            "CAST(json_extract(config_json, '$.owner_user_id') AS TEXT)=?",
        ]
        params: list[object] = [owner_user_id]
        if account_id:
            where.append("id=?")
            params.append(str(account_id))
        limit = ""
        if item_limit is not None:
            limit = " LIMIT ?"
            params.append(max(1, int(item_limit)))
        rows = self.connection.execute(
            """
            SELECT id, account_name, base_currency, initial_cash, cash_balance,
                   status, execution_mode, config_json, created_at, updated_at
            FROM paper_accounts
            WHERE """ + " AND ".join(where)
            + " ORDER BY datetime(updated_at) DESC, id DESC" + limit,
            tuple(params),
        ).fetchall()
        return [
            account for row in rows
            if (account := self._account(
                row, owner_user_id=owner_user_id, history_limit=history_limit
            ))
        ]

    def _metric_rows(self, run_id: str) -> tuple[dict, list]:
        rows = self.connection.execute(
            """
            SELECT metric_key, metric_value, metric_text, unit, metadata_json
            FROM backtest_metrics WHERE backtest_run_id=? ORDER BY metric_key
            """,
            (run_id,),
        ).fetchall()
        metrics = {
            str(row[0]): {
                "value": float(row[1]) if row[1] is not None else None,
                "text": str(row[2] or ""), "unit": str(row[3] or ""),
                "metadata": _json_object(row[4]),
            }
            for row in rows
        }
        curve = _json_list((metrics.get("equity_curve") or {}).get("text"))
        return metrics, curve

    def _related_orders(self, *, owner_user_id: str, report_id: int) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT orders.id, account.id, account.account_name, orders.status,
                   instrument.canonical_symbol, instrument.display_name,
                   orders.side, orders.order_type, orders.quantity,
                   orders.submitted_at, orders.completed_at
            FROM paper_orders orders
            JOIN paper_accounts account ON account.id=orders.account_id
            JOIN financial_instruments instrument ON instrument.id=orders.instrument_id
            WHERE json_valid(account.config_json)
              AND CAST(json_extract(account.config_json, '$.owner_user_id') AS TEXT)=?
              AND orders.final_report_id=?
            ORDER BY datetime(orders.submitted_at) DESC, orders.id DESC
            LIMIT 100
            """,
            (owner_user_id, int(report_id)),
        ).fetchall()
        return [
            {
                "order_id": str(row[0]), "account_id": str(row[1]),
                "account_name": str(row[2]), "status": str(row[3]),
                "canonical_symbol": str(row[4]), "display_name": str(row[5]),
                "side": str(row[6]), "order_type": str(row[7]),
                "quantity": float(row[8]), "submitted_at": str(row[9] or ""),
                "completed_at": str(row[10] or ""),
                "final_report_id": int(report_id),
                "report_url": f"/api/financial/reports/{int(report_id)}",
            }
            for row in rows
        ]

    def _backtest(self, row, *, owner_user_id: str, trade_limit: int | None) -> dict | None:
        config = _json_object(row[9])
        if str(config.get("owner_user_id") or "") != owner_user_id:
            return None
        run_id = str(row[0])
        count = int(self.connection.execute(
            "SELECT COUNT(*) FROM backtest_trades WHERE backtest_run_id=?", (run_id,)
        ).fetchone()[0])
        query = """
            SELECT trades.id, trades.instrument_id, instrument.canonical_symbol,
                   instrument.display_name, trades.side, trades.quantity,
                   trades.price, trades.fee, trades.signal_at, trades.executed_at,
                   trades.reason_json
            FROM backtest_trades trades
            JOIN financial_instruments instrument ON instrument.id=trades.instrument_id
            WHERE trades.backtest_run_id=? ORDER BY trades.id
        """
        params: tuple = (run_id,)
        if trade_limit is not None:
            query += " LIMIT ?"
            params = (run_id, int(trade_limit))
        trade_rows = self.connection.execute(query, params).fetchall()
        trades = []
        for item in trade_rows:
            reason = _json_object(item[10])
            snapshot_id = reason.get("snapshot_id")
            try:
                snapshot_id = int(snapshot_id) if snapshot_id is not None else None
            except (TypeError, ValueError):
                snapshot_id = None
            trades.append(
                {
                    "trade_id": int(item[0]), "instrument_id": int(item[1]),
                    "canonical_symbol": str(item[2]), "display_name": str(item[3]),
                    "side": str(item[4]), "quantity": float(item[5]),
                    "price": float(item[6]), "fee": float(item[7]),
                    "signal_at": str(item[8] or ""), "executed_at": str(item[9]),
                    "reason": reason, "snapshot_id": snapshot_id,
                    "evidence_url": f"/api/financial/snapshots/{snapshot_id}" if snapshot_id else "",
                }
            )
        metrics, equity_curve = self._metric_rows(run_id)
        coverage = config.get("coverage") if isinstance(config.get("coverage"), Mapping) else {}
        source_report_id = _report_id(config)
        scope_name = str(row[17] or row[18] or "")
        evidence = []
        for item in list(config.get("snapshot_manifest") or ()):
            if not isinstance(item, Mapping):
                continue
            try:
                snapshot_id = int(item.get("snapshot_id"))
            except (TypeError, ValueError):
                continue
            evidence.append(
                {
                    "snapshot_id": snapshot_id,
                    "payload_sha256": str(item.get("payload_sha256") or ""),
                    "observed_at": str(item.get("observed_at") or ""),
                    "url": f"/api/financial/snapshots/{snapshot_id}",
                }
            )
        return {
            "backtest_run_id": run_id, "strategy_key": str(row[1]),
            "strategy_version": str(row[2]), "scope_type": str(row[3]),
            "instrument_id": int(row[4]) if row[4] is not None else None,
            "universe_id": int(row[5]) if row[5] is not None else None,
            "scope_name": scope_name, "start_date": str(row[6]), "end_date": str(row[7]),
            "initial_capital": float(row[8]), "status": str(row[10]),
            "data_cutoff_at": str(row[11] or ""), "last_error": str(row[12] or ""),
            "created_at": str(row[13]), "started_at": str(row[14] or ""),
            "completed_at": str(row[15] or ""), "base_currency": str(config.get("base_currency") or ""),
            "data_version": str(config.get("data_version") or ""),
            "coverage": dict(coverage), "limitations": list(coverage.get("limitations") or ()),
            "metrics": metrics, "equity_curve": equity_curve, "trades": trades,
            "trade_count": count, "trades_truncated": trade_limit is not None and count > len(trades),
            "evidence": evidence, "source_report_id": source_report_id,
            "report_url": f"/api/financial/reports/{source_report_id}" if source_report_id else "",
            "execution_mode": "paper", "real_order_execution": False,
            "disclaimer": "历史回测仅供研究参考，不代表未来表现，不构成投资建议。",
            "export_url": f"/api/intel/financial/simulation/export?kind=backtest&id={run_id}",
        }

    def _backtests(
        self, *, owner_user_id: str, trade_limit: int | None = DEFAULT_TRADE_LIMIT,
        run_id: str = "", item_limit: int | None = MAX_BACKTEST_ITEMS,
        source_report_id: int | None = None,
    ) -> list[dict]:
        where = [
            "json_valid(run.config_json)",
            "CAST(json_extract(run.config_json, '$.owner_user_id') AS TEXT)=?",
        ]
        params: list[object] = [owner_user_id]
        if run_id:
            where.append("run.id=?")
            params.append(str(run_id))
        if source_report_id is not None:
            report_paths = (
                "$.source_report_id", "$.final_report_id",
                "$.strategy_parameters.source_report_id",
                "$.strategy_parameters.final_report_id",
            )
            where.append("(" + " OR ".join(
                f"CAST(json_extract(run.config_json, '{path}') AS INTEGER)=?"
                for path in report_paths
            ) + ")")
            params.extend([int(source_report_id)] * len(report_paths))
        limit = ""
        if item_limit is not None:
            limit = " LIMIT ?"
            params.append(max(1, int(item_limit)))
        rows = self.connection.execute(
            """
            SELECT run.id, run.strategy_key, run.model_version, run.scope_type,
                   run.instrument_id, run.universe_id, run.start_date, run.end_date,
                   run.initial_capital, run.config_json, run.status,
                   run.data_cutoff_at, run.last_error, run.created_at,
                   run.started_at, run.completed_at, run.config_json,
                   instrument.display_name, universe.display_name
            FROM backtest_runs run
            LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
            LEFT JOIN financial_universes universe ON universe.id=run.universe_id
            WHERE """ + " AND ".join(where)
            + " ORDER BY datetime(run.created_at) DESC, run.id DESC" + limit,
            tuple(params),
        ).fetchall()
        return [
            item for row in rows
            if (item := self._backtest(row, owner_user_id=owner_user_id, trade_limit=trade_limit))
        ]

    def build(
        self, *, owner_user_id: object, industry_pack_id: str,
        mode: str = "simulation", report_id: int | None = None,
        trade_limit: int = DEFAULT_TRADE_LIMIT,
    ) -> dict:
        owner = str(owner_user_id or "").strip()
        if not owner:
            raise PermissionError("模拟页面必须关联登录用户")
        normalized_mode = str(mode or "simulation").strip().casefold()
        if normalized_mode not in {"simulation", "backtesting"}:
            raise ValueError("mode 只支持 simulation 或 backtesting")
        limit = max(1, min(MAX_TRADE_LIMIT, int(trade_limit)))
        state = financial_product_capabilities(
            str(industry_pack_id or ""), settings=self.settings
        )
        capability = normalized_mode
        wanted_report = int(report_id) if report_id else None
        with self.database.lock:
            accounts = self._accounts(owner_user_id=owner)
            backtests = self._backtests(owner_user_id=owner, trade_limit=limit)
            related_orders = self._related_orders(
                owner_user_id=owner, report_id=wanted_report
            ) if wanted_report else []
            related_backtests = self._backtests(
                owner_user_id=owner, trade_limit=limit,
                source_report_id=wanted_report, item_limit=MAX_BACKTEST_ITEMS,
            ) if wanted_report else []
        can_create = bool(state["product"][capability])
        has_history = bool(accounts or backtests)
        return {
            "view_version": FINANCIAL_SIMULATION_VIEW_VERSION,
            "visible": bool(can_create or has_history), "mode": normalized_mode,
            "industry_pack_id": str(industry_pack_id or ""),
            "can_create": can_create,
            "capability_reason": state["product_reasons"][capability],
            "running_task_policy": state["running_task_policy"],
            "execution_mode": "paper", "real_order_execution": False,
            "accounts": accounts, "backtests": backtests,
            "counts": {"accounts": len(accounts), "backtests": len(backtests)},
            "report_id": wanted_report,
            "related": {"paper_orders": related_orders, "backtests": related_backtests},
            "disclaimer": "全部账户、成交和回测均为纸面模拟；历史表现不代表未来结果。",
        }

    def export(self, *, owner_user_id: object, kind: str, item_id: str) -> dict:
        owner = str(owner_user_id or "").strip()
        if not owner:
            raise PermissionError("模拟导出必须关联登录用户")
        normalized = str(kind or "").strip().casefold()
        with self.database.lock:
            if normalized == "account":
                item = next(iter(self._accounts(
                    owner_user_id=owner, account_id=str(item_id),
                    history_limit=None, item_limit=None,
                )), None)
            elif normalized == "backtest":
                item = next(iter(self._backtests(
                    owner_user_id=owner, trade_limit=None, run_id=str(item_id),
                    item_limit=None,
                )), None)
            else:
                raise ValueError("kind 只支持 account 或 backtest")
        if item is None:
            raise PermissionError("导出对象不存在或无权访问")
        return {
            "view_version": FINANCIAL_SIMULATION_VIEW_VERSION,
            "export_kind": normalized, "exported_item": item,
            "execution_mode": "paper", "real_order_execution": False,
            "disclaimer": "全部账户、成交和回测均为纸面模拟；历史表现不代表未来结果。",
        }


__all__ = [
    "DEFAULT_TRADE_LIMIT", "FINANCIAL_SIMULATION_VIEW_VERSION",
    "FinancialSimulationView", "MAX_ACCOUNT_ITEMS", "MAX_BACKTEST_ITEMS",
    "MAX_TRADE_LIMIT",
]
