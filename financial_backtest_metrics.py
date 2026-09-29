#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Recomputable analytics and auditable trade logs for persisted backtests."""

from __future__ import annotations

import json
import math
import statistics
import threading
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, time, timezone
from decimal import Decimal

from financial_backtest import (
    BacktestError,
    FinancialPointInTimeBacktester,
    _canonical_json,
    _date,
    _decimal,
    _money,
    _sha,
    _utc,
    _utc_text,
)
from financial_config import require_financial_product_capability


FINANCIAL_BACKTEST_ANALYTICS_VERSION = "financial-backtest-analytics-v1"
MIN_RISK_RETURN_OBSERVATIONS = 20


class FinancialBacktestAnalytics:
    """Compute metrics from the immutable run contract and saved trade log."""

    def __init__(self, database, *, settings=None):
        self.database = database
        self.settings = settings
        self.reader = FinancialPointInTimeBacktester(database, settings=settings)
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

    def _run(self, run_id: str, owner_user_id: object) -> dict:
        row = self.connection.execute(
            """
            SELECT id, strategy_key, model_version, scope_type, instrument_id,
                   universe_id, start_date, end_date, initial_capital,
                   config_json, status, data_cutoff_at, completed_at
            FROM backtest_runs WHERE id=?
            """,
            (str(run_id or ""),),
        ).fetchone()
        if row is None:
            raise BacktestError("回测不存在", error_code="backtest_not_found")
        config = json.loads(str(row[9] or "{}"))
        if not isinstance(config, Mapping):
            raise BacktestError("回测配置损坏", error_code="backtest_config_invalid")
        config = dict(config)
        owner = str(owner_user_id or "")
        if str(config.get("owner_user_id") or "") != owner:
            raise PermissionError("无权访问该回测")
        if str(row[10]) != "completed":
            raise BacktestError("回测尚未完成", error_code="backtest_not_completed")
        return {
            "backtest_run_id": str(row[0]), "strategy_key": str(row[1]),
            "strategy_version": str(row[2]), "scope_type": str(row[3]),
            "instrument_id": int(row[4]) if row[4] is not None else None,
            "universe_id": int(row[5]) if row[5] is not None else None,
            "start_date": str(row[6]), "end_date": str(row[7]),
            "initial_capital": _decimal(row[8], "initial_capital", positive=True),
            "config": config, "data_cutoff_at": str(row[11]),
            "completed_at": str(row[12] or ""),
        }

    def _trades(self, run_id: str) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT id, instrument_id, side, quantity, price, fee,
                   signal_at, executed_at, reason_json
            FROM backtest_trades WHERE backtest_run_id=? ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            try:
                reason = json.loads(str(row[8] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise BacktestError(
                    "交易日志原因字段损坏", error_code="backtest_trade_log_invalid"
                ) from exc
            if not isinstance(reason, Mapping):
                raise BacktestError(
                    "交易日志原因字段损坏", error_code="backtest_trade_log_invalid"
                )
            result.append(
                {
                    "trade_id": int(row[0]), "instrument_id": int(row[1]),
                    "side": str(row[2]), "quantity": float(row[3]),
                    "price": float(row[4]), "fee": float(row[5]),
                    "signal_at": str(row[6] or ""), "executed_at": str(row[7]),
                    "reason": dict(reason),
                }
            )
        return result

    def _verified_inputs(self, run: Mapping[str, object]):
        config = dict(run["config"])
        cutoff = _utc(config.get("data_cutoff_at"), "data_cutoff_at")
        snapshot_ids = config.get("snapshot_ids") or ()
        bars, _, manifest = self.reader._load_snapshot_data(
            snapshot_ids, data_cutoff_at=cutoff
        )
        benchmark_manifest = None
        benchmark_bars = {}
        benchmark_snapshot_id = config.get("benchmark_snapshot_id")
        if benchmark_snapshot_id:
            benchmark_bars, _, benchmark_items = self.reader._load_snapshot_data(
                [int(benchmark_snapshot_id)], data_cutoff_at=cutoff
            )
            benchmark_manifest = benchmark_items[0]
        saved_manifest = config.get("snapshot_manifest")
        if manifest != saved_manifest or benchmark_manifest != config.get("benchmark_manifest"):
            raise BacktestError(
                "回测数据清单已变化", error_code="backtest_data_manifest_changed"
            )
        universe = config.get("universe_manifest")
        universe_members = list(universe.get("members") or ()) if isinstance(universe, Mapping) else []
        data_version = _sha(
            {
                "snapshots": manifest, "benchmark": benchmark_manifest,
                "universe_members": universe_members,
                "data_cutoff_at": _utc_text(cutoff),
            }
        )
        if data_version != str(config.get("data_version") or ""):
            raise BacktestError(
                "回测数据版本校验失败", error_code="backtest_data_version_changed"
            )
        return bars, benchmark_bars, data_version

    @staticmethod
    def _metric(
        value, *, unit: str, formula: str, input_hash: str,
        status: str = "available", sample_count: int = 0, text: str = "",
        extra: Mapping | None = None,
    ) -> dict:
        if value is not None:
            value = float(value)
            if not math.isfinite(value):
                raise BacktestError("回测指标不是有限数", error_code="non_finite_metric")
        metadata = {
            "analytics_version": FINANCIAL_BACKTEST_ANALYTICS_VERSION,
            "input_hash": input_hash, "formula": formula,
            "status": status, "sample_count": int(sample_count),
            **dict(extra or {}),
        }
        return {
            "value": value, "text": str(text or ("" if value is not None else status)),
            "unit": str(unit), "metadata": metadata,
        }

    def _load_saved_metrics(self, run_id: str) -> dict:
        rows = self.connection.execute(
            """
            SELECT metric_key, metric_value, metric_text, unit, metadata_json
            FROM backtest_metrics WHERE backtest_run_id=? ORDER BY metric_key
            """,
            (run_id,),
        ).fetchall()
        result = {}
        for row in rows:
            try:
                metadata = json.loads(str(row[4] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise BacktestError(
                    "回测指标元数据损坏", error_code="backtest_metric_invalid"
                ) from exc
            result[str(row[0])] = {
                "value": float(row[1]) if row[1] is not None else None,
                "text": str(row[2] or ""), "unit": str(row[3] or ""),
                "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
            }
        return result

    def calculate(self, run_id: str, *, owner_user_id: object) -> dict:
        run = self._run(run_id, owner_user_id)
        config = dict(run["config"])
        require_financial_product_capability(
            "backtesting", str(config.get("industry_pack_id") or ""),
            settings=self.settings,
        )
        bars, benchmark_bars, data_version = self._verified_inputs(run)
        trades = self._trades(run["backtest_run_id"])
        trade_log_sha256 = _sha(trades)
        input_hash = _sha(
            {
                "analytics_version": FINANCIAL_BACKTEST_ANALYTICS_VERSION,
                "request_fingerprint": config.get("request_fingerprint"),
                "data_version": data_version, "trade_log_sha256": trade_log_sha256,
                "corporate_action_audit": config.get("corporate_action_audit") or [],
            }
        )
        saved = self._load_saved_metrics(run["backtest_run_id"])
        if saved:
            saved_hashes = {
                str(item["metadata"].get("input_hash") or "") for item in saved.values()
            }
            if saved_hashes != {input_hash}:
                raise BacktestError(
                    "回测交易日志或指标输入已变化",
                    error_code="backtest_trade_log_changed",
                )
            return self._result(
                run, trades, saved, input_hash=input_hash,
                trade_log_sha256=trade_log_sha256, idempotent=True,
            )

        start = _date(run["start_date"], "start_date")
        end = _date(run["end_date"], "end_date")
        start_at = datetime.combine(start, time.min, tzinfo=timezone.utc)
        end_at = datetime.combine(end, time.max, tzinfo=timezone.utc)
        filtered = {
            instrument: [item for item in values if start_at <= item.observed_at <= end_at]
            for instrument, values in bars.items()
        }
        events = {
            item.observed_at for values in filtered.values() for item in values
            if item.available_at <= item.observed_at
        }
        for trade in trades:
            events.add(_utc(trade["executed_at"], "trade_executed_at"))
        event_times = sorted(events)
        if not event_times:
            raise BacktestError("无法重建回测时间轴", error_code="backtest_timeline_missing")

        action_items = []
        for raw in config.get("corporate_action_audit") or ():
            if not isinstance(raw, Mapping):
                raise BacktestError("公司行动审计损坏", error_code="corporate_action_audit_invalid")
            action_items.append(
                {
                    "action_key": str(raw.get("action_key") or ""),
                    "instrument_id": int(raw.get("instrument_id")),
                    "action_type": str(raw.get("action_type") or ""),
                    "effective_at": _utc(raw.get("effective_at"), "action_effective_at"),
                    "available_at": _utc(raw.get("available_at"), "action_available_at"),
                    "value": _decimal(raw.get("value"), "action_value", positive=True),
                }
            )
        action_items.sort(key=lambda item: (item["effective_at"], item["available_at"], item["action_key"]))
        trades_by_time: dict[datetime, list[dict]] = defaultdict(list)
        for trade in trades:
            trades_by_time[_utc(trade["executed_at"], "trade_executed_at")].append(trade)

        cash = Decimal(str(run["initial_capital"]))
        positions: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        cost_basis: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        cash_income: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        processed_actions: set[str] = set()
        round_trip_pnls: list[Decimal] = []
        gross_notional = Decimal("0")
        fee_total = Decimal("0")
        slippage_total = Decimal("0")
        daily_equity: dict[str, dict] = {}

        for event_at in event_times:
            for action in action_items:
                if action["action_key"] in processed_actions:
                    continue
                if action["effective_at"] > event_at or action["available_at"] > event_at:
                    continue
                processed_actions.add(action["action_key"])
                instrument = action["instrument_id"]
                quantity = positions[instrument]
                if quantity <= 0:
                    continue
                if action["action_type"] == "split":
                    positions[instrument] = quantity * action["value"]
                elif action["action_type"] == "dividend":
                    income = quantity * action["value"]
                    cash += income
                    cash_income[instrument] += income

            for trade in trades_by_time.get(event_at, ()):
                instrument = int(trade["instrument_id"])
                quantity = _decimal(trade["quantity"], "trade_quantity", positive=True)
                price = _decimal(trade["price"], "trade_price", positive=True)
                fee = _decimal(trade["fee"], "trade_fee")
                if fee < 0:
                    raise BacktestError("交易费用无效", error_code="backtest_trade_log_invalid")
                notional = quantity * price
                gross_notional += notional
                fee_total += fee
                if trade["side"] == "buy":
                    cash -= notional + fee
                    positions[instrument] += quantity
                    cost_basis[instrument] += notional + fee
                elif trade["side"] == "sell":
                    held = positions[instrument]
                    if held + Decimal("0.00000001") < quantity:
                        raise BacktestError(
                            "交易日志卖出超过持仓", error_code="backtest_trade_log_invalid"
                        )
                    allocated_cost = (
                        cost_basis[instrument] * quantity / held if held > 0 else Decimal("0")
                    )
                    allocated_income = (
                        cash_income[instrument] * quantity / held if held > 0 else Decimal("0")
                    )
                    proceeds = notional - fee
                    round_trip_pnls.append(proceeds + allocated_income - allocated_cost)
                    cash += proceeds
                    positions[instrument] = max(Decimal("0"), held - quantity)
                    cost_basis[instrument] = max(
                        Decimal("0"), cost_basis[instrument] - allocated_cost
                    )
                    cash_income[instrument] = max(
                        Decimal("0"), cash_income[instrument] - allocated_income
                    )
                else:
                    raise BacktestError("交易方向无效", error_code="backtest_trade_log_invalid")
                reason = trade["reason"]
                snapshot_id = int(reason.get("snapshot_id") or 0)
                price_field = str(reason.get("price_field") or "")
                candidates = [
                    item for item in filtered.get(instrument, ())
                    if item.snapshot_id == snapshot_id and item.observed_at == event_at
                    and item.available_at <= event_at
                ]
                if not candidates or price_field not in {"open", "close"}:
                    raise BacktestError(
                        "交易日志无法回指执行快照", error_code="trade_snapshot_lineage_invalid"
                    )
                source_bar = max(candidates, key=lambda item: item.available_at)
                raw_price = source_bar.open if price_field == "open" else source_bar.close
                slippage_total += abs(price - raw_price) * quantity

            market_value = Decimal("0")
            marks = {}
            for instrument, quantity in sorted(positions.items()):
                if quantity <= 0:
                    continue
                known = self.reader._known_bars_as_of(filtered.get(instrument, ()), event_at)
                if not known:
                    raise BacktestError(
                        "持仓缺少时点估值", error_code="position_valuation_missing"
                    )
                mark = known[-1].close
                market_value += quantity * mark
                marks[str(instrument)] = _money(mark)
            equity = cash + market_value
            if not equity.is_finite():
                raise BacktestError("净值不是有限数", error_code="non_finite_equity")
            daily_equity[event_at.date().isoformat()] = {
                "as_of": _utc_text(event_at), "cash": _money(cash),
                "market_value": _money(market_value), "equity": _money(equity),
                "marks": marks,
            }

        equity_curve = [daily_equity[key] for key in sorted(daily_equity)]
        equities = [Decimal(str(item["equity"])) for item in equity_curve]
        if not equities:
            raise BacktestError("净值曲线为空", error_code="equity_curve_missing")
        risk_equities = [Decimal(str(run["initial_capital"])), *equities]
        returns = [
            float(risk_equities[index] / risk_equities[index - 1] - Decimal("1"))
            for index in range(1, len(risk_equities))
            if risk_equities[index - 1] != 0
        ]
        initial_capital = Decimal(str(run["initial_capital"]))
        ending_equity = equities[-1]
        total_return = ending_equity / initial_capital - Decimal("1")
        peak = initial_capital
        max_drawdown = Decimal("0")
        for equity in risk_equities:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = min(max_drawdown, equity / peak - Decimal("1"))
        elapsed_days = max(0, (end - start).days)
        annualized_return = None
        annualized_status = "insufficient_sample"
        if elapsed_days >= 30 and ending_equity > 0:
            annualized_return = math.pow(float(ending_equity / initial_capital), 365 / elapsed_days) - 1
            annualized_status = "available"
        volatility = sharpe = None
        risk_status = "insufficient_sample"
        if len(returns) >= MIN_RISK_RETURN_OBSERVATIONS:
            daily_stdev = statistics.stdev(returns)
            volatility = daily_stdev * math.sqrt(252)
            if daily_stdev > 0:
                sharpe = statistics.mean(returns) / daily_stdev * math.sqrt(252)
                risk_status = "available"
            else:
                risk_status = "zero_volatility"
        win_rate = None
        win_status = "no_closed_trades"
        if round_trip_pnls:
            win_rate = sum(1 for item in round_trip_pnls if item > 0) / len(round_trip_pnls)
            win_status = "available"
        average_equity = sum(equities) / Decimal(len(equities))
        turnover = gross_notional / average_equity if average_equity > 0 else None

        benchmark_return = relative_return = None
        benchmark_status = "benchmark_missing"
        if benchmark_bars:
            benchmark_values = next(iter(benchmark_bars.values()))
            known = [
                item for item in benchmark_values
                if start_at <= item.observed_at <= end_at and item.available_at <= item.observed_at
            ]
            known = self.reader._known_bars_as_of(known, end_at)
            if len(known) >= 2 and known[0].close > 0:
                benchmark_return = float(known[-1].close / known[0].close - Decimal("1"))
                relative_return = float(total_return) - benchmark_return
                benchmark_status = "available"

        coverage = config.get("coverage") if isinstance(config.get("coverage"), Mapping) else {}
        coverage_ratio = float(coverage.get("coverage_ratio") or 0)
        shared = {
            "data_version": data_version, "trade_log_sha256": trade_log_sha256,
            "coverage_ratio": coverage_ratio,
        }
        metrics = {
            "total_return": self._metric(
                total_return, unit="ratio", formula="ending_equity/initial_capital-1",
                input_hash=input_hash, sample_count=len(equities), extra=shared,
            ),
            "annualized_return": self._metric(
                annualized_return, unit="ratio",
                formula="(ending_equity/initial_capital)^(365/elapsed_days)-1",
                input_hash=input_hash, status=annualized_status,
                sample_count=len(equities), extra=shared,
            ),
            "max_drawdown": self._metric(
                max_drawdown, unit="ratio", formula="min(equity/running_peak-1)",
                input_hash=input_hash, sample_count=len(equities), extra=shared,
            ),
            "annualized_volatility": self._metric(
                volatility, unit="ratio", formula="sample_stdev(daily_returns)*sqrt(252)",
                input_hash=input_hash, status=risk_status,
                sample_count=len(returns), extra=shared,
            ),
            "sharpe_ratio": self._metric(
                sharpe, unit="ratio",
                formula="mean(daily_returns)/sample_stdev(daily_returns)*sqrt(252);risk_free=0",
                input_hash=input_hash, status=risk_status,
                sample_count=len(returns), extra=shared,
            ),
            "win_rate": self._metric(
                win_rate, unit="ratio", formula="profitable_closed_sells/closed_sells",
                input_hash=input_hash, status=win_status,
                sample_count=len(round_trip_pnls), extra=shared,
            ),
            "turnover": self._metric(
                turnover, unit="ratio", formula="gross_trade_notional/average_equity",
                input_hash=input_hash,
                status="available" if turnover is not None else "non_positive_average_equity",
                sample_count=len(trades), extra=shared,
            ),
            "fee_total": self._metric(
                fee_total, unit=str(config.get("base_currency") or ""),
                formula="sum(trade_fee)", input_hash=input_hash,
                sample_count=len(trades), extra=shared,
            ),
            "slippage_cost_total": self._metric(
                slippage_total, unit=str(config.get("base_currency") or ""),
                formula="sum(abs(execution_price*pinned_raw_price_difference)*quantity)",
                input_hash=input_hash, sample_count=len(trades), extra=shared,
            ),
            "transaction_cost_total": self._metric(
                fee_total + slippage_total, unit=str(config.get("base_currency") or ""),
                formula="fee_total+slippage_cost_total", input_hash=input_hash,
                sample_count=len(trades), extra=shared,
            ),
            "benchmark_return": self._metric(
                benchmark_return, unit="ratio", formula="benchmark_last_close/first_close-1",
                input_hash=input_hash, status=benchmark_status,
                sample_count=0 if benchmark_return is None else 2, extra=shared,
            ),
            "relative_return": self._metric(
                relative_return, unit="ratio", formula="total_return-benchmark_return",
                input_hash=input_hash, status=benchmark_status,
                sample_count=0 if relative_return is None else 2, extra=shared,
            ),
            "ending_equity": self._metric(
                ending_equity, unit=str(config.get("base_currency") or ""),
                formula="cash+sum(position_quantity*point_in_time_close)",
                input_hash=input_hash, sample_count=len(equities), extra=shared,
            ),
            "trade_count": self._metric(
                len(trades), unit="count", formula="count(backtest_trades)",
                input_hash=input_hash, sample_count=len(trades), extra=shared,
            ),
            "closed_trade_count": self._metric(
                len(round_trip_pnls), unit="count", formula="count(sell_trade_pnl)",
                input_hash=input_hash, sample_count=len(round_trip_pnls), extra=shared,
            ),
            "data_coverage_ratio": self._metric(
                coverage_ratio, unit="ratio", formula="usable_point_in_time_observations/expected_observations",
                input_hash=input_hash, sample_count=int(coverage.get("usable_point_in_time_observations") or 0),
                extra={**shared, "limitations": list(coverage.get("limitations") or ())},
            ),
            "trade_log_sha256": self._metric(
                None, unit="sha256", formula="sha256(canonical_trade_log)",
                input_hash=input_hash, status="available", sample_count=len(trades),
                text=trade_log_sha256, extra=shared,
            ),
            "equity_curve": self._metric(
                None, unit="json", formula="daily_last(cash+marked_positions)",
                input_hash=input_hash, status="available", sample_count=len(equity_curve),
                text=_canonical_json(equity_curve), extra=shared,
            ),
        }
        now = datetime.now(timezone.utc)
        with self.lock:
            self.connection.execute("SAVEPOINT backtest_metrics_atomic")
            try:
                if self.connection.execute(
                    "SELECT COUNT(*) FROM backtest_metrics WHERE backtest_run_id=?",
                    (run["backtest_run_id"],),
                ).fetchone()[0]:
                    raise BacktestError(
                        "回测指标已被并发写入", error_code="backtest_metrics_race"
                    )
                for metric_key, item in sorted(metrics.items()):
                    self.connection.execute(
                        """
                        INSERT INTO backtest_metrics(
                            backtest_run_id, metric_key, metric_value,
                            metric_text, unit, metadata_json, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run["backtest_run_id"], metric_key, item["value"], item["text"],
                            item["unit"], _canonical_json(item["metadata"]), _utc_text(now),
                        ),
                    )
                self.connection.execute("RELEASE SAVEPOINT backtest_metrics_atomic")
            except Exception:
                self.connection.execute("ROLLBACK TO SAVEPOINT backtest_metrics_atomic")
                self.connection.execute("RELEASE SAVEPOINT backtest_metrics_atomic")
                raise
        return self._result(
            run, trades, metrics, input_hash=input_hash,
            trade_log_sha256=trade_log_sha256, idempotent=False,
        )

    @staticmethod
    def _result(
        run: Mapping[str, object], trades: list[dict], metrics: Mapping[str, object],
        *, input_hash: str, trade_log_sha256: str, idempotent: bool,
    ) -> dict:
        equity_metric = metrics.get("equity_curve") or {}
        try:
            equity_curve = json.loads(str(equity_metric.get("text") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            equity_curve = []
        return {
            "backtest_run_id": str(run["backtest_run_id"]),
            "analytics_version": FINANCIAL_BACKTEST_ANALYTICS_VERSION,
            "input_hash": input_hash, "trade_log_sha256": trade_log_sha256,
            "metrics": dict(metrics), "equity_curve": equity_curve,
            "trades": trades, "idempotent": bool(idempotent),
            "execution_mode": "paper", "real_order_execution": False,
            "disclaimer": "历史回测仅供研究参考，不代表未来表现，不构成投资建议。",
        }


__all__ = [
    "FINANCIAL_BACKTEST_ANALYTICS_VERSION", "FinancialBacktestAnalytics",
    "MIN_RISK_RETURN_OBSERVATIONS",
]
