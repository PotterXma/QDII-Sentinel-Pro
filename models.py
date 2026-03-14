"""
QDII 哨兵 Pro 数据库模块
SQLite + WAL 模式 + 线程安全 + 历史记录
表: funds / fund_history / exchange_rates / fund_detail / push_log
"""

import sqlite3
import threading
import logging
from datetime import datetime

from config import DB_PATH

logger = logging.getLogger(__name__)

_db_lock = threading.Lock()


def _get_conn(db_path=None):
    """获取数据库连接（线程安全）"""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path=None):
    """初始化数据库表"""
    conn = _get_conn(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS funds (
                code        TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                limit_amount REAL DEFAULT -1.0,
                limit_text  TEXT DEFAULT '',
                current_nav REAL DEFAULT 0.0,
                day_growth  REAL DEFAULT 0.0,
                last_update TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS fund_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT NOT NULL,
                name        TEXT DEFAULT '',
                old_limit   REAL,
                new_limit   REAL,
                old_text    TEXT DEFAULT '',
                new_text    TEXT DEFAULT '',
                changed_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS exchange_rates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                currency    TEXT DEFAULT 'USD/CNY',
                rate        REAL NOT NULL,
                recorded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fund_detail (
                code           TEXT PRIMARY KEY,
                manager        TEXT DEFAULT '',
                fund_size      REAL DEFAULT 0,
                fee_rate       REAL DEFAULT 0,
                max_drawdown   REAL DEFAULT 0,
                top_holdings   TEXT DEFAULT '',
                nav_history    TEXT DEFAULT '',
                score          REAL DEFAULT 0,
                last_deep_scan TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS push_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_code   TEXT NOT NULL,
                limit_amount REAL NOT NULL,
                status      TEXT NOT NULL,
                pushed_at   TEXT NOT NULL,
                channel     TEXT DEFAULT 'bark'
            );

            CREATE INDEX IF NOT EXISTS idx_history_code ON fund_history(code);
            CREATE INDEX IF NOT EXISTS idx_history_time ON fund_history(changed_at);
            CREATE INDEX IF NOT EXISTS idx_fx_time ON exchange_rates(recorded_at);
            CREATE INDEX IF NOT EXISTS idx_push_log_code ON push_log(fund_code);
        """)
        conn.commit()
        logger.info("数据库初始化完成")
    finally:
        conn.close()


# ── funds 表操作 ─────────────────────────────────────────


def get_fund(code, db_path=None):
    """查询单只基金"""
    conn = _get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM funds WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_fund(data, db_path=None):
    """
    插入或更新基金数据。
    如果 limit_amount 发生变化，同时写入 fund_history。
    返回变动信息字典，无变动则返回 None。
    """
    code = data["code"]
    name = data["name"]
    limit_amount = data["limit_amount"]
    limit_text = data.get("limit_text", "")
    current_nav = data.get("current_nav", 0.0)
    day_growth = data.get("day_growth", 0.0)
    last_update = data.get("last_update", datetime.now().strftime("%Y-%m-%d %H:%M"))

    change_info = None

    with _db_lock:
        conn = _get_conn(db_path)
        try:
            existing = conn.execute(
                "SELECT * FROM funds WHERE code = ?", (code,)
            ).fetchone()

            if existing:
                old_limit = existing["limit_amount"]
                old_text = existing["limit_text"]

                if abs(old_limit - limit_amount) > 0.001:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        """INSERT INTO fund_history
                           (code, name, old_limit, new_limit, old_text, new_text, changed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (code, name, old_limit, limit_amount, old_text, limit_text, now),
                    )
                    change_info = {
                        "code": code,
                        "name": name,
                        "old_limit": old_limit,
                        "new_limit": limit_amount,
                        "old_text": old_text,
                        "new_text": limit_text,
                    }
                    logger.info(
                        "限额变动: %s %s  %.2f → %.2f",
                        code, name, old_limit, limit_amount,
                    )

                conn.execute(
                    """UPDATE funds SET name=?, limit_amount=?, limit_text=?,
                       current_nav=?, day_growth=?, last_update=?
                       WHERE code=?""",
                    (name, limit_amount, limit_text, current_nav, day_growth, last_update, code),
                )
            else:
                conn.execute(
                    """INSERT INTO funds (code, name, limit_amount, limit_text,
                       current_nav, day_growth, last_update)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (code, name, limit_amount, limit_text, current_nav, day_growth, last_update),
                )
                change_info = {
                    "code": code,
                    "name": name,
                    "old_limit": None,
                    "new_limit": limit_amount,
                    "old_text": "",
                    "new_text": limit_text,
                }

            conn.commit()
        finally:
            conn.close()

    return change_info


def get_all_funds(order_by_limit=True, db_path=None):
    """获取所有基金，默认按限额降序"""
    conn = _get_conn(db_path)
    try:
        order = "ORDER BY limit_amount DESC" if order_by_limit else "ORDER BY code"
        rows = conn.execute(f"SELECT * FROM funds {order}").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_fund_history(code=None, limit=50, db_path=None):
    """查询变动历史"""
    conn = _get_conn(db_path)
    try:
        if code:
            rows = conn.execute(
                "SELECT * FROM fund_history WHERE code = ? ORDER BY changed_at DESC LIMIT ?",
                (code, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM fund_history ORDER BY changed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent_changes(hours=24, db_path=None):
    """获取最近 N 小时的变动"""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM fund_history
               WHERE changed_at >= datetime('now', '-' || ? || ' hours', 'localtime')
               ORDER BY changed_at DESC""",
            (hours,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── exchange_rates 表操作 ────────────────────────────────


def save_exchange_rate(rate, currency="USD/CNY", db_path=None):
    """保存汇率记录"""
    with _db_lock:
        conn = _get_conn(db_path)
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO exchange_rates (currency, rate, recorded_at) VALUES (?, ?, ?)",
                (currency, rate, now),
            )
            conn.commit()
            logger.info("汇率已保存: %s = %.4f", currency, rate)
        finally:
            conn.close()


def get_latest_rate(currency="USD/CNY", db_path=None):
    """获取最新汇率"""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM exchange_rates WHERE currency = ? ORDER BY recorded_at DESC LIMIT 1",
            (currency,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_rate_change(days=30, currency="USD/CNY", db_path=None):
    """计算最近 N 天的汇率变动百分比"""
    conn = _get_conn(db_path)
    try:
        latest = conn.execute(
            "SELECT rate FROM exchange_rates WHERE currency = ? ORDER BY recorded_at DESC LIMIT 1",
            (currency,),
        ).fetchone()
        oldest = conn.execute(
            """SELECT rate FROM exchange_rates
               WHERE currency = ? AND recorded_at >= datetime('now', '-' || ? || ' days', 'localtime')
               ORDER BY recorded_at ASC LIMIT 1""",
            (currency, days),
        ).fetchone()
        if latest and oldest and oldest["rate"] > 0:
            return (latest["rate"] - oldest["rate"]) / oldest["rate"]
        return 0.0
    finally:
        conn.close()


# ── fund_detail 表操作 ───────────────────────────────────


def upsert_fund_detail(data, db_path=None):
    """插入或更新基金详细信息"""
    code = data["code"]
    with _db_lock:
        conn = _get_conn(db_path)
        try:
            existing = conn.execute(
                "SELECT code FROM fund_detail WHERE code = ?", (code,)
            ).fetchone()

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if existing:
                conn.execute(
                    """UPDATE fund_detail SET manager=?, fund_size=?, fee_rate=?,
                       max_drawdown=?, top_holdings=?, nav_history=?,
                       score=?, last_deep_scan=?
                       WHERE code=?""",
                    (
                        data.get("manager", ""),
                        data.get("fund_size", 0),
                        data.get("fee_rate", 0),
                        data.get("max_drawdown", 0),
                        data.get("top_holdings", ""),
                        data.get("nav_history", ""),
                        data.get("score", 0),
                        now,
                        code,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO fund_detail
                       (code, manager, fund_size, fee_rate, max_drawdown,
                        top_holdings, nav_history, score, last_deep_scan)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        code,
                        data.get("manager", ""),
                        data.get("fund_size", 0),
                        data.get("fee_rate", 0),
                        data.get("max_drawdown", 0),
                        data.get("top_holdings", ""),
                        data.get("nav_history", ""),
                        data.get("score", 0),
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()


def get_fund_detail(code, db_path=None):
    """获取基金详细信息"""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM fund_detail WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_fund_details(db_path=None):
    """获取所有基金详细信息"""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM fund_detail ORDER BY score DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_funds_with_details(order_by="score", db_path=None):
    """获取基金列表 + 详情联合查询，带评分"""
    conn = _get_conn(db_path)
    try:
        order_clause = {
            "score": "COALESCE(d.score, 0) DESC",
            "limit": "f.limit_amount DESC",
            "name": "f.name ASC",
        }.get(order_by, "COALESCE(d.score, 0) DESC")

        rows = conn.execute(
            f"""SELECT f.*, COALESCE(d.score, 0) as score,
                       COALESCE(d.fund_size, 0) as fund_size,
                       COALESCE(d.fee_rate, 0) as fee_rate,
                       COALESCE(d.max_drawdown, 0) as max_drawdown,
                       COALESCE(d.manager, '') as manager,
                       COALESCE(d.last_deep_scan, '') as last_deep_scan
                FROM funds f
                LEFT JOIN fund_detail d ON f.code = d.code
                ORDER BY {order_clause}"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── push_log 推送去重 ────────────────────────────────────


def should_push(code, limit_amount, status, db_path=None):
    """
    判断是否应该推送。
    仅当 (fund_code, limit_amount, status) 与数据库中
    该基金最后一条推送记录不同时，才返回 True。
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            """SELECT limit_amount, status FROM push_log
               WHERE fund_code = ? ORDER BY pushed_at DESC LIMIT 1""",
            (code,),
        ).fetchone()
        if not row:
            return True  # 从未推送过
        if abs(row["limit_amount"] - limit_amount) > 0.001:
            return True  # 限额变动
        if row["status"] != status:
            return True  # 状态变动
        return False
    finally:
        conn.close()


def record_push(code, limit_amount, status, channel="bark", db_path=None):
    """记录一条推送记录，用于后续去重判断。"""
    with _db_lock:
        conn = _get_conn(db_path)
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT INTO push_log
                   (fund_code, limit_amount, status, pushed_at, channel)
                   VALUES (?, ?, ?, ?, ?)""",
                (code, limit_amount, status, now, channel),
            )
            conn.commit()
        finally:
            conn.close()

