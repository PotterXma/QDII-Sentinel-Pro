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

import os
import sys

def resource_path(relative_path):
    """获取资源文件的绝对路径 (用于 PyInstaller 套件)"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static")
)

_last_scan_time = "尚未扫描"
_scan_lock = threading.Lock()
_is_scanning = False


def set_last_scan_time(t):
    global _last_scan_time
    _last_scan_time = t


def set_scanning_state(state):
    """供 main.py 调用，标记扫描状态"""
    global _is_scanning
    _is_scanning = state


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
    fx_rate = fx.get("rate") if fx else None
    fx_time = fx.get("recorded_at") if fx else None

    # 统计
    all_funds = get_all_funds()
    open_count = sum(1 for f in all_funds if f["limit_amount"] > 0)
    paused_count = sum(1 for f in all_funds if f["limit_amount"] == 0.0)

    # 如果 last_scan_time 仍为初始值，尝试从数据库最新记录推断
    display_scan_time = _last_scan_time
    if display_scan_time == "尚未扫描" and all_funds:
        latest_update = max((f["last_update"] for f in all_funds if f.get("last_update")), default="")
        if latest_update:
            display_scan_time = latest_update

    return render_template(
        "index.html",
        funds=funds,
        query=q,
        sort_by=sort_by,
        last_scan=display_scan_time,
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


@app.route("/refresh_deep", methods=["POST"])
def refresh_deep():
    """手动触发深度扫描（后台线程执行）"""
    global _is_scanning

    with _scan_lock:
        if _is_scanning:
            return jsonify({"status": "already_running", "message": "扫描正在进行中"}), 409
        _is_scanning = True

    def _do_deep_scan():
        global _is_scanning
        try:
            from deep_scanner import run_deep_scan
            from scorer import update_all_scores

            run_deep_scan()
            update_all_scores()
        except Exception as e:
            logger.error("手动深度扫描失败: %s", str(e))
        finally:
            _is_scanning = False

    t = threading.Thread(target=_do_deep_scan, daemon=True)
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
    """JSON 状态接口 — 含完整统计信息"""
    all_funds = get_all_funds()
    fx = get_latest_rate()

    display_scan_time = _last_scan_time
    if display_scan_time == "尚未扫描" and all_funds:
        latest_update = max((f["last_update"] for f in all_funds if f.get("last_update")), default="")
        if latest_update:
            display_scan_time = latest_update

    recent = get_recent_changes(hours=24)

    return jsonify({
        "last_scan": display_scan_time,
        "fund_count": len(all_funds),
        "is_scanning": _is_scanning,
        "fx_rate": fx.get("rate") if fx else None,
        "fx_time": fx.get("recorded_at") if fx else None,
        "open_count": sum(1 for f in all_funds if f["limit_amount"] > 0),
        "paused_count": sum(1 for f in all_funds if f["limit_amount"] == 0.0),
        "change_count": len(recent),
    })


# ── 基金类型分类 ─────────────────────────────────────────

_BOND_KEYWORDS = [
    "债券", "债", "收益", "利率", "信用", "纯债", "短债", "定期",
    "增强回报", "双利", "稳健",
]
_STOCK_KEYWORDS = [
    "纳斯达克", "纳指", "标普", "道琼斯", "罗素", "科技", "芯片",
    "半导体", "消费", "医药", "医疗", "生物", "指数", "股票",
    "成长", "价值", "混合", "量化", "精选", "优选", "ETF",
    "油气", "能源", "黄金", "商品", "资源", "互联网", "新经济",
    "全球配置", "世纪", "港股", "中概",
]


def _classify_fund_type(name):
    """根据基金名称关键词分类为 bond / stock"""
    if not name:
        return "stock"
    for kw in _BOND_KEYWORDS:
        if kw in name:
            return "bond"
    return "stock"


@app.route("/api/funds")
def api_funds():
    """JSON 基金数据接口 — 供前端静默刷新"""
    sort_by = request.args.get("sort", "score")
    q = request.args.get("q", "").strip()

    funds = get_funds_with_details(order_by=sort_by)

    if q:
        funds = [
            f for f in funds
            if q.lower() in f["name"].lower() or q in f["code"]
        ]

    import json

    result = []
    for f in funds:
        nav_history_str = f.get("nav_history", "[]")
        try:
            nav_history = json.loads(nav_history_str) if nav_history_str else []
        except Exception:
            nav_history = []

        # 计算多区间涨幅
        gw_1w = gw_1m = gw_3m = 0.0
        if nav_history:
            current_nav = nav_history[0]["nav"]
            
            # 近一周 (约 5 个交易日)
            if len(nav_history) >= 5:
                gw_1w = (current_nav - nav_history[4]["nav"]) / nav_history[4]["nav"]
            # 近一月 (约 20 个交易日)
            if len(nav_history) >= 20:
                gw_1m = (current_nav - nav_history[19]["nav"]) / nav_history[19]["nav"]
            elif len(nav_history) > 5:
                gw_1m = (current_nav - nav_history[-1]["nav"]) / nav_history[-1]["nav"]  # 用尽量多的数据
            # 近三月 (约 60 个交易日)
            if len(nav_history) >= 60:
                gw_3m = (current_nav - nav_history[59]["nav"]) / nav_history[59]["nav"]
            elif len(nav_history) > 20:
                gw_3m = (current_nav - nav_history[-1]["nav"]) / nav_history[-1]["nav"]
                
        result.append({
            "code": f["code"],
            "name": f["name"],
            "score": f.get("score", 0) or 0,
            "limit_amount": f["limit_amount"],
            "limit_text": f.get("limit_text", ""),
            "current_nav": f.get("current_nav", 0),
            "day_growth": f.get("day_growth", 0),
            "1w_growth": gw_1w,
            "1m_growth": gw_1m,
            "3m_growth": gw_3m,
            "fund_size": f.get("fund_size", 0),
            "last_update": f.get("last_update", ""),
            "fund_type": _classify_fund_type(f["name"]),
        })

    return jsonify(result)
