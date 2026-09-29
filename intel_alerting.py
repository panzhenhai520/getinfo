# -*- coding: utf-8 -*-
"""生产健康告警：定时检查 worker 未跑 / 远端VPN不可达 / 候选堆积，达到阈值通过邮件通知。

防抖：VPN 不可达与候选堆积需「连续 N 次检查」确认后才发邮件（避免间歇超时/短暂峰值刷屏），
恢复后立刻复位。N 可通过环境变量调整：
  SYSTEM_ALERT_REMOTE_FAILS（默认 3，约 15 分钟连续不可达才告警）
  SYSTEM_ALERT_BACKLOG_CONFIRM（默认 2，连续两次超阈值才告警）
  SYSTEM_ALERT_BACKLOG（堆积阈值，默认 60）
worker 离线属于紧急事件，保持单次即告警。
"""
import os
import time
import threading

_state = {'worker_alerted': False, 'remote_fails': 0, 'remote_alerted': False,
          'backlog_fails': 0, 'backlog_alerted': False}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, '') or default))
    except (TypeError, ValueError):
        return default


def _collect_status():
    """返回 (engine_snapshot, remote_health, candidate_queued, running_jobs)。失败时安全返回默认值。"""
    try:
        from firecrawl_app import _engine_status_snapshot, _remote_pipeline_health
        eng = _engine_status_snapshot()
        rp = _remote_pipeline_health()
        pi = eng.get('pipeline') or {}
        return eng, rp, int(pi.get('candidate_queued') or 0), int(pi.get('running_jobs') or 0)
    except Exception:
        return {}, {}, 0, 0


def _send_mail(to, subject, body):
    try:
        from pack_tenant import _send_email
        ok = _send_email(to, f'【情报系统告警】{subject}', body)
        print(f"[alert] 邮件已发送 -> {to}: {subject}" if ok else f"[alert] 邮件提交失败 {to}: {subject}")
        return ok
    except Exception as exc:
        print(f"[alert] 邮件发送异常: {exc}")
        return False


def check_and_alert(alert_email: str = '', backlog_threshold: int = 60) -> None:
    """检查三项健康，发现问题且达到阈值时发送邮件告警。

    worker 判定用引擎 active（存活/活跃），不用 running_jobs（健康但空闲 ≠ 宕机，避免误报）。
    VPN/堆积需连续 N 次确认（防抖），恢复即复位。
    """
    remote_fails_required = _env_int('SYSTEM_ALERT_REMOTE_FAILS', 3)
    backlog_confirm_required = _env_int('SYSTEM_ALERT_BACKLOG_CONFIRM', 2)
    eng, rp, cand, running = _collect_status()
    issues = []
    # 1) worker 未运行：以引擎 active 判定（worker 存活/活跃）为准——紧急，单次即告警
    if not eng.get('active'):
        if not _state['worker_alerted']:
            _state['worker_alerted'] = True
            issues.append('信源 Worker 未运行/已离线（引擎不活跃，候选可能堆积）—— 可能 worker 容器未启动或已崩溃，请检查 Docker 与容器日志')
    else:
        _state['worker_alerted'] = False
    # 2) 远端 VPN pipeline 不可达：连续 N 次才告警（健康检查 8s 超时，忙时可能偶发）
    if not rp.get('ok'):
        _state['remote_fails'] += 1
        if not _state['remote_alerted'] and _state['remote_fails'] >= remote_fails_required:
            _state['remote_alerted'] = True
            issues.append(f'远端 VPN Pipeline 不可达（连续 {_state["remote_fails"]} 次）：{rp.get("error") or "未连通"}')
    else:
        _state['remote_fails'] = 0
        _state['remote_alerted'] = False
    # 3) 候选堆积：连续 M 次超阈值才告警（短暂峰值不刷屏）
    if cand >= backlog_threshold:
        _state['backlog_fails'] += 1
        if not _state['backlog_alerted'] and _state['backlog_fails'] >= backlog_confirm_required:
            _state['backlog_alerted'] = True
            issues.append(f'候选堆积 {cand} 条 >= {backlog_threshold} 阈值（连续 {_state["backlog_fails"]} 次）')
    else:
        _state['backlog_fails'] = 0
        _state['backlog_alerted'] = False

    if issues:
        body = ('检测到以下问题，请尽快处理：\n\n- ' + '\n- '.join(issues) +
                f'\n\n时间：{time.strftime("%Y-%m-%d %H:%M:%S")}')
        print('[alert] ' + ' | '.join(issues), flush=True)
        if alert_email:
            _send_mail(alert_email, '多用户情报系统告警', body)


def start_alert_loop(interval_seconds: int = 300):
    """后台告警线程：每 interval_seconds 检查一次。"""
    alert_email = str(os.getenv('SYSTEM_ALERT_EMAIL', '') or '').strip()
    backlog = int(os.getenv('SYSTEM_ALERT_BACKLOG', '60') or '60')

    def _loop():
        while True:
            try:
                check_and_alert(alert_email, backlog)
            except Exception as exc:
                print(f'[alert] 检查异常: {exc}', flush=True)
            time.sleep(interval_seconds)

    th = threading.Thread(target=_loop, name='intel-alert-monitor', daemon=True)
    th.start()
    print(f'[alert] 告警监控已启动（每 {interval_seconds}s，收件 {alert_email or "未配置"}，'
          f'候选阈值 {backlog}，VPN 连续 {_env_int("SYSTEM_ALERT_REMOTE_FAILS", 3)} 次、'
          f'堆积连续 {_env_int("SYSTEM_ALERT_BACKLOG_CONFIRM", 2)} 次确认才告警）', flush=True)
    return th
