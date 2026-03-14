"""
QDII Watcher Flask Web 仪表盘
搜索 / 手动刷新 / 历史变动 / 评分 / 汇率 / 状态 API
"""

import threading
import logging
from flask import Flask, render_template, request, jsonify, redirect, url_for

from models import (
    init_db, get_all_funds, get_fund_history, get_recent_changes,
    get_funds_with_details, get_latest_rate,
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

_last_scan_time = "尚未扫描"
_scan_lock = threading.Lock()
_is_scanning = False


def set_last_scan_time(t):
    global _last_scan_time
    _last_scan_time = t


def get_last_scan_time():
    return _last_scan_time


# ── 路由 ─────────────────────────────────────────────────


@app.route("/")
def index():
    """基金列表主页，支持 ?q= 搜索 + ?sort= 排序"""
    q = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "score")  # score / limit / name

    funds = get_funds_with_details(order_by=sort_by)

    if q:
        funds = [
            f for f in funds
            if q.lower() in f["name"].lower() or q in f["code"]
        ]

    recent = get_recent_changes(hours=24)

    # 汇率
    fx = get_latest_rate()
    fx_rate = fx["rate"] if fx else None
    fx_time = fx["recorded_at"] if fx else None

    # 统计
    all_funds = get_all_funds()
    open_count = sum(1 for f in all_funds if f["limit_amount"] > 0)
    paused_count = sum(1 for f in all_funds if f["limit_amount"] == 0.0)

    return render_template(
        "index.html",
        funds=funds,
        query=q,
        sort_by=sort_by,
        last_scan=_last_scan_time,
        recent_changes=recent[:10],
        is_scanning=_is_scanning,
        total_count=len(all_funds),
        open_count=open_count,
        paused_count=paused_count,
        fx_rate=fx_rate,
        fx_time=fx_time,
    )


@app.route("/refresh", methods=["POST"])
def refresh():
    """手动触发扫描（后台线程执行）"""
    global _is_scanning

    with _scan_lock:
        if _is_scanning:
            return jsonify({"status": "already_running", "message": "扫描正在进行中"}), 409
        _is_scanning = True  # 在锁内、启动线程前设置，消除竞态窗口

    def _do_scan():
        global _is_scanning
        try:
            from scraper import run_full_scan
            from datetime import datetime

            run_full_scan()
            set_last_scan_time(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            logger.error("手动扫描失败: %s", str(e))
        finally:
            _is_scanning = False

    t = threading.Thread(target=_do_scan, daemon=True)
    t.start()

    return redirect(url_for("index"))


@app.route("/history")
def history():
    """变动历史页"""
    code = request.args.get("code", "").strip()
    limit_num = int(request.args.get("limit", "100"))

    if code:
        records = get_fund_history(code=code, limit=limit_num)
    else:
        records = get_fund_history(limit=limit_num)

    return render_template(
        "history.html",
        records=records,
        code_filter=code,
        last_scan=_last_scan_time,
    )


@app.route("/api/status")
def api_status():
    """JSON 状态接口"""
    funds = get_all_funds()
    fx = get_latest_rate()
    return jsonify({
        "last_scan": _last_scan_time,
        "fund_count": len(funds),
        "is_scanning": _is_scanning,
        "fx_rate": fx["rate"] if fx else None,
    })
