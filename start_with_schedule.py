#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
增强版启动脚本 - 确保定时任务正常工作
专门用于Docker容器环境
"""

import os
import sys
import time
import signal
import logging
import threading
from datetime import datetime
from threading import Thread


def _load_dotenv_file(path='.env'):
    if not os.path.exists(path):
        return

    try:
        with open(path, 'r', encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[7:].strip()
                key, value = line.split('=', 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = value.strip().strip('"').strip("'")
    except Exception as exc:
        print(f"Warning: failed to load .env: {exc}")


_load_dotenv_file()


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


_configure_stdio()

# 时区支持
try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        import pytz
        ZoneInfo = None

def get_china_time():
    """获取中国时区的当前时间（UTC+8 / Asia/Shanghai）"""
    if ZoneInfo is not None:
        try:
            china_tz = ZoneInfo('Asia/Shanghai')
            return datetime.now(china_tz).replace(tzinfo=None)
        except Exception:
            pass

    import pytz
    china_tz = pytz.timezone('Asia/Shanghai')
    return datetime.now(china_tz).replace(tzinfo=None)

def setup_logging():
    """设置日志"""
    handlers = [logging.StreamHandler(sys.stdout)]
    log_file = os.getenv('LOG_FILE', 'app.log')
    if log_file:
        try:
            log_dir = os.path.dirname(log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding='utf-8'))
        except Exception as exc:
            print(f"Warning: log file disabled ({log_file}): {exc}")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers
    )

def check_environment():
    """检查环境配置"""
    print("🔍 检查容器环境...")
    
    # 检查时区
    print(f"⏰ 当前时区: {os.environ.get('TZ', 'Not Set')}")
    china_time = get_china_time()
    print(f"🕐 系统时间: {datetime.now()}")
    print(f"🇨🇳 中国时间: {china_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 检查数据库
    try:
        from sqlite_database import sqlite_db
        if sqlite_db.connect():
            print("✅ SQLite数据库连接正常")
            
            # 检查定时任务
            tasks, total = sqlite_db.get_scheduled_tasks(1, 100, is_active=True)
            print(f"📋 发现 {total} 个活跃定时任务")
            
            if total > 0:
                print("📝 定时任务列表:")
                for task in tasks[:5]:  # 只显示前5个
                    print(f"   - {task.get('task_name', 'N/A')}: {task.get('schedule_time', 'N/A')}")
        else:
            print("❌ SQLite数据库连接失败")
    except Exception as e:
        print(f"❌ 数据库检查失败: {e}")
    
    # 检查Redis连接
    try:
        import redis
        redis_host = os.getenv('REDIS_HOST', 'firecrawl-redis')
        redis_port = int(os.getenv('REDIS_PORT', 6379))
        
        r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True, socket_connect_timeout=5)
        r.ping()
        print(f"✅ Redis连接正常: {redis_host}:{redis_port}")
    except Exception as e:
        print(f"⚠️ Redis连接失败: {e}")

def schedule_monitor():
    """定时任务监控线程"""
    print("🔄 启动定时任务监控线程...")
    
    while True:
        try:
            from sqlite_database import sqlite_db
            # 使用中国时区时间
            now = get_china_time()
            
            # 检查活跃任务
            tasks, total = sqlite_db.get_scheduled_tasks(1, 100, is_active=True)
            
            if total > 0:
                print(f"📊 [{now.strftime('%H:%M:%S')}] 监控中: {total} 个活跃定时任务")
                
                # 检查是否有任务应该在这个时间点执行
                current_time = f"{now.hour:02d}:{now.minute:02d}"
                for task in tasks:
                    task_time = task.get('schedule_time', '')[:5]  # 只取HH:MM部分
                    if task_time == current_time:
                        print(f"🚨 定时任务触发警报: {task.get('task_name')} 应该在 {current_time} 执行!")
            
            time.sleep(300)  # 每5分钟检查一次
            
        except Exception as e:
            print(f"❌ 定时任务监控异常: {e}")
            time.sleep(60)

_shutdown_requested = threading.Event()


def _force_exit_after_timeout(signum, timeout_seconds):
    """Guarantee systemd can restart even if a worker ignores stop flags."""
    time.sleep(timeout_seconds)
    print(f"⚠️ 关闭超过 {timeout_seconds} 秒，强制退出旧进程")
    os._exit(128 + int(signum))


def _start_force_exit_watchdog(signum):
    try:
        timeout_seconds = int(os.getenv('SHUTDOWN_FORCE_EXIT_SECONDS', '15') or 15)
    except (TypeError, ValueError):
        timeout_seconds = 15
    timeout_seconds = max(1, min(60, timeout_seconds))
    watchdog = Thread(
        target=_force_exit_after_timeout,
        args=(signum, timeout_seconds),
        name='ShutdownForceExitWatchdog',
        daemon=True,
    )
    watchdog.start()


def signal_handler(signum, frame):
    """信号处理器"""
    if _shutdown_requested.is_set():
        os._exit(128 + int(signum))
    _shutdown_requested.set()
    print(f"\n🛑 收到信号 {signum}，正在关闭应用...")
    _start_force_exit_watchdog(signum)
    try:
        from scheduler import scheduler

        scheduler.request_stop(reason='process_shutdown')
    except Exception as exc:
        print(f"⚠️ 调度器停止请求失败: {exc}")
    sys.exit(0)

def main():
    """主函数"""
    print("🚀 启动增强版firecrawl应用（支持定时任务）")
    print("=" * 50)
    
    # 设置信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 设置日志
    setup_logging()
    
    # 检查环境
    check_environment()
    
    # 启动监控线程
    monitor_thread = Thread(target=schedule_monitor)
    monitor_thread.daemon = True
    monitor_thread.start()
    
    # 等待一下确保所有初始化完成
    print("⏳ 等待初始化完成...")
    time.sleep(3)
    
    try:
        # 导入并启动Flask应用
        print("🌐 启动Flask应用...")
        from firecrawl_app import app
        
        # 确保定时任务线程启动
        try:
            from firecrawl_app import _ensure_schedule_thread
            if os.getenv('ENABLE_SCHEDULER', 'true').strip().lower() in ('1', 'true', 'yes', 'on'):
                _ensure_schedule_thread()
                print("✅ 定时任务线程已启动")
            else:
                print("⏸️ ENABLE_SCHEDULER=false，Web 进程不启动调度器（由独立 firecrawl-scheduler 服务运行）")
        except Exception as e:
            print(f"⚠️ 定时任务线程启动警告: {e}")
        
        print("🎉 应用启动完成！")
        # 启动生产健康告警监控（worker未跑/远程不可达/候选堆积 → 邮件）
        try:
            from intel_alerting import start_alert_loop
            start_alert_loop(int(os.getenv('SYSTEM_ALERT_INTERVAL', '300') or '300'))
        except Exception as e:
            print(f"⚠️ 告警监控启动警告: {e}")
        flask_host = os.getenv('FLASK_HOST', '0.0.0.0')
        flask_port = int(os.getenv('FLASK_PORT', 8003))
        print(f"🌐 访问地址: http://{flask_host}:{flask_port}")
        print(f"📊 定时任务监控: 已启用")
        
        # 启动Flask应用
        app.run(debug=False, host=flask_host, port=flask_port)
        
    except Exception as e:
        print(f"💥 应用启动失败: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
