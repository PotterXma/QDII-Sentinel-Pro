"""
QDII 哨兵 Pro 主入口
架构:
  主线程 ─── pystray 托盘循环
  守护线程1 ── APScheduler (基础扫描/深度扫描/汇率更新)
  守护线程2 ── Flask 看板 (127.0.0.1:5000)
  单实例锁 ── socket 端口占用检测
"""

import os
import sys
import socket
import logging
import webbrowser
import threading
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime

import pystray
from PIL import Image, ImageDraw
from apscheduler.schedulers.background import BackgroundScheduler

from config import (
    SCHEDULE_HOURS, DEEP_SCAN_HOURS, FX_UPDATE_HOURS,
    FLASK_PORT, SINGLETON_PORT, LOG_DIR, DATA_DIR,
    DAILY_PUSH_HOUR, DAILY_PUSH_MINUTE,
)
from models import init_db
from scraper import run_full_scan
from deep_scanner import run_deep_scan
from fx_tracker import update_exchange_rate
from scorer import update_all_scores, get_top5_recommendations
from notifier import send_daily_top5
from app import app, set_last_scan_time, set_scanning_state


# ── 单实例锁 ─────────────────────────────────────────────

_lock_socket = None


def acquire_singleton_lock():
    """
    通过绑定本地端口实现单实例检测。
    成功返回 True，端口已被占用返回 False。
    """
    global _lock_socket
    _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock_socket.bind(("127.0.0.1", SINGLETON_PORT))
        _lock_socket.listen(1)
        return True
    except OSError:
        _lock_socket.close()
        _lock_socket = None
        return False


def release_singleton_lock():
    """释放单实例锁"""
    global _lock_socket
    if _lock_socket:
        try:
            _lock_socket.close()
        except Exception:
            pass
        _lock_socket = None


# ── 日志配置 ─────────────────────────────────────────────


def setup_logging():
    """配置日志: 文件轮转 (写入 APPDATA) + 控制台 (非 --noconsole 时)"""
    log_file = os.path.join(LOG_DIR, "qdii_sentinel.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)-20s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    from logging.handlers import RotatingFileHandler
    
    # 文件处理器 — 按大小轮转 (单文件最大 10MB，保留 5 个备份)，防止撑爆硬盘
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)

    # 控制台处理器 — 仅在非冻结（非 pyinstaller --noconsole）模式下输出
    if not getattr(sys, 'frozen', False):
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(fmt)
        root_logger.addHandler(console)

    # 降低第三方库日志等级
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ── 托盘图标 ─────────────────────────────────────────────


def _create_tray_icon_image():
    """动态生成 64x64 绿色圆点托盘图标（无需外部 ICO 文件）"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 外圈深绿
    draw.ellipse([4, 4, 60, 60], fill=(46, 204, 113), outline=(39, 174, 96), width=2)
    # 内圈浅绿（高光）
    draw.ellipse([18, 18, 46, 46], fill=(88, 214, 141))
    return img


# ── 定时任务 ─────────────────────────────────────────────


def task_basic_scan():
    """基础扫描: 限额 + 净值"""
    try:
        set_scanning_state(True)
        logger.info("[定时] 基础扫描开始")
        run_full_scan()
        set_last_scan_time(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("[定时] 基础扫描完成")
    except Exception as e:
        logger.error("[定时] 基础扫描异常: %s", str(e))
    finally:
        set_scanning_state(False)


def task_deep_scan():
    """深度扫描: 历史净值 / 持仓 / 费率 → 评分"""
    try:
        set_scanning_state(True)
        logger.info("[定时] 深度扫描开始")
        run_deep_scan()
        update_all_scores()
        logger.info("[定时] 深度扫描 + 评分更新完成")
    except Exception as e:
        logger.error("[定时] 深度扫描异常: %s", str(e))
    finally:
        set_scanning_state(False)


def task_fx_update():
    """汇率更新 → 评分"""
    try:
        logger.info("[定时] 汇率更新开始")
        rate = update_exchange_rate()
        if rate:
            update_all_scores()
            logger.info("[定时] 汇率更新完成: USD/CNY=%.4f", rate)
        else:
            logger.warning("[定时] 汇率更新失败")
    except Exception as e:
        logger.error("[定时] 汇率更新异常: %s", str(e))


def task_daily_top5():
    """每日 TOP5 推荐推送"""
    try:
        logger.info("[定时] 每日 TOP5 推送开始")
        top5 = get_top5_recommendations()
        send_daily_top5(top5)
        logger.info("[定时] 每日 TOP5 推送完成")
    except Exception as e:
        logger.error("[定时] 每日 TOP5 推送异常: %s", str(e))


# ── 托盘菜单回调 ─────────────────────────────────────────


_scheduler = None


def _on_open_dashboard(icon, item):
    """打开看板"""
    webbrowser.open(f"http://127.0.0.1:{FLASK_PORT}")


def _on_manual_scan(icon, item):
    """手动触发一次扫描"""
    logger.info("[手动] 开始基础扫描...")
    t = threading.Thread(target=task_basic_scan, daemon=True)
    t.start()


def _on_manual_deep_scan(icon, item):
    """手动触发一次深度扫描"""
    logger.info("[手动] 开始深度扫描...")
    t = threading.Thread(target=task_deep_scan, daemon=True)
    t.start()


def _on_exit(icon, item):
    """退出系统 — 按顺序清理所有资源"""
    global _scheduler
    logger.info("收到退出信号，正在清理...")

    # 1. 停止调度器
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("调度器已停止")

    # 2. 释放单实例锁
    release_singleton_lock()
    logger.info("单实例锁已释放")

    # 3. 停止托盘
    icon.stop()
    logger.info("QDII 哨兵 Pro 已发出退出指令")
    
    # 4. 强制杀死本进程（防止 Flask / Schedule 守护线程出现孤儿或僵尸）
    os._exit(0)


# ── Flask 守护线程 ───────────────────────────────────────


def _run_flask():
    """在守护线程中运行 Flask"""
    app.run(
        host="127.0.0.1",
        port=FLASK_PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )


# ── 主入口 ───────────────────────────────────────────────


def main():
    global _scheduler

    setup_logging()

    logger.info("=" * 60)
    logger.info("QDII 哨兵 Pro 启动中...")
    logger.info("  数据目录: %s", DATA_DIR)
    logger.info("  日志目录: %s", LOG_DIR)
    logger.info("  基础扫描: 每%dh | 深度扫描: 每%dh | 汇率: 每%dh",
                SCHEDULE_HOURS, DEEP_SCAN_HOURS, FX_UPDATE_HOURS)
    logger.info("  Flask 端口: %d", FLASK_PORT)
    logger.info("=" * 60)

    # 单实例检测
    if not acquire_singleton_lock():
        logger.error("检测到已有实例运行 (端口 %d 已被占用)", SINGLETON_PORT)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "QDII 哨兵 Pro 已在运行中！\n请检查系统托盘。",
                "QDII 哨兵 Pro", 0x30
            )
        except Exception:
            pass
        sys.exit(1)

    # 初始化数据库
    init_db()

    # 首次任务放入后台线程（防止网络不通时阻塞托盘显示）
    def _initial_tasks():
        logger.info("执行首次汇率更新...")
        task_fx_update()
        logger.info("执行首次基础扫描...")
        task_basic_scan()
        logger.info("执行首次深度扫描 (用于初始化近三月基准净值)...")
        task_deep_scan()
        logger.info("首次所有初始化任务完成")

    init_thread = threading.Thread(target=_initial_tasks, daemon=True, name="InitTasks")
    init_thread.start()

    # 设置三路调度（守护线程）
    _scheduler = BackgroundScheduler(daemon=True)

    _scheduler.add_job(
        task_basic_scan, "interval",
        hours=SCHEDULE_HOURS,
        id="basic_scan",
        name=f"基础扫描 (每{SCHEDULE_HOURS}h)",
    )
    _scheduler.add_job(
        task_deep_scan, "interval",
        hours=DEEP_SCAN_HOURS,
        id="deep_scan",
        name=f"深度扫描 (每{DEEP_SCAN_HOURS}h)",
    )
    _scheduler.add_job(
        task_fx_update, "interval",
        hours=FX_UPDATE_HOURS,
        id="fx_update",
        name=f"汇率更新 (每{FX_UPDATE_HOURS}h)",
    )
    _scheduler.add_job(
        task_daily_top5, "cron",
        hour=DAILY_PUSH_HOUR, minute=DAILY_PUSH_MINUTE,
        id="daily_top5",
        name=f"每日TOP5推送 ({DAILY_PUSH_HOUR}:{DAILY_PUSH_MINUTE:02d})",
    )
    _scheduler.start()
    logger.info("四路定时任务已启动 (含每日 %d:%02d TOP5)", DAILY_PUSH_HOUR, DAILY_PUSH_MINUTE)

    # 启动 Flask（守护线程）
    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask 看板: http://127.0.0.1:%d", FLASK_PORT)

    # 创建系统托盘（主线程阻塞）
    icon = pystray.Icon(
        name="QDII_Sentinel",
        icon=_create_tray_icon_image(),
        title="QDII 哨兵 Pro",
        menu=pystray.Menu(
            pystray.MenuItem("📊 打开看板", _on_open_dashboard, default=True),
            pystray.MenuItem("🔄 基础扫描", _on_manual_scan),
            pystray.MenuItem("🔍 深度扫描", _on_manual_deep_scan),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ 退出系统", _on_exit),
        ),
    )

    logger.info("系统托盘已就绪 — QDII 哨兵 Pro 运行中")
    icon.run()  # 阻塞主线程


if __name__ == "__main__":
    main()
