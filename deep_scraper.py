"""
QDII Sentinel Pro 深度爬虫模块
历史净值 / 持仓 / 费率 / 最大回撤 — 并发处理 + 分批 + 反爬保护
"""

import re
import time
import random
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    FUND_NAV_HISTORY_URL,
    FUND_HOLDINGS_URL,
    FUND_FEE_URL,
    USER_AGENTS,
    MAG7_TICKERS,
    DEEP_SCAN_BATCH_SIZE,
    DEEP_SCAN_BATCH_DELAY,
    DEEP_SCAN_REQUEST_DELAY,
    DEEP_SCAN_MAX_FAILURES,
    DEEP_SCAN_WORKERS,
)
from models import (
    get_all_funds,
    save_nav_history,
    save_holdings,
    update_fund_deep_data,
    get_nav_history,
)

logger = logging.getLogger(__name__)


# ── HTTP 工具 ────────────────────────────────────────────


def _create_session():
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": "http://fund.eastmoney.com/",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def _delay():
    time.sleep(random.uniform(*DEEP_SCAN_REQUEST_DELAY))


# ── 历史净值抓取 ─────────────────────────────────────────


def fetch_nav_history(session, code, days=365):
    """
    从天天基金 F10DataApi 抓取历史净值。
    返回: list of (nav_date, nav, acc_nav)

    API 返回格式: var apidata={ content:"<table ...>...</table>", records:N, pages:M }
    """
    # 请求足够多的数据（每页最多40条比较稳定）
    per = min(days, 40)
    pages_needed = (days // per) + 1
    all_navs = []

    for page in range(1, pages_needed + 1):
        url = FUND_NAV_HISTORY_URL.format(code=code, per=per)
        url = url.replace("page=1", f"page={page}")
        try:
            resp = session.get(url, headers=_random_headers(), timeout=20)
            resp.raise_for_status()
            content = resp.text

            # 解析 HTML 表格中的净值数据
            # 格式: <tr><td>2026-03-12</td><td class='tor bold'>5.4310</td><td class='tor bold'>5.4310</td>...
            rows = re.findall(
                r"<tr><td>(\d{4}-\d{2}-\d{2})</td>"
                r"<td class='tor bold'>([\d.]+)</td>"
                r"<td class='tor bold'>([\d.]+)</td>",
                content,
            )
            if not rows:
                break

            for date_str, nav_str, acc_nav_str in rows:
                try:
                    all_navs.append((date_str, float(nav_str), float(acc_nav_str)))
                except ValueError:
                    continue

            # 检查是否还有更多页
            records_match = re.search(r"records:(\d+)", content)
            pages_match = re.search(r"pages:(\d+)", content)
            if pages_match and page >= int(pages_match.group(1)):
                break

            _delay()

        except Exception as e:
            logger.warning("抓取 %s 第%d页净值失败: %s", code, page, str(e))
            break

    if all_navs:
        save_nav_history(code, all_navs)
        logger.debug("保存 %s 历史净值 %d 条", code, len(all_navs))

    return all_navs


# ── 持仓抓取 ─────────────────────────────────────────────


def fetch_holdings(session, code):
    """
    从天天基金抓取前十大持仓。
    返回: (report_date, list of {stock_code, stock_name, hold_ratio, rank})

    API 返回原始 HTML 表格，需要解析 <td> 中的持仓比例。
    """
    url = FUND_HOLDINGS_URL.format(code=code)
    try:
        resp = session.get(url, headers=_random_headers(), timeout=20)
        resp.raise_for_status()
        content = resp.text

        # 检查是否有数据
        content_match = re.search(r'content:"(.*?)",arryear', content, re.DOTALL)
        if not content_match or not content_match.group(1).strip():
            return "", []

        html = content_match.group(1)

        # 提取报告期日期
        date_match = re.search(r"截止至：(\d{4}-\d{2}-\d{2})", html)
        report_date = date_match.group(1) if date_match else ""

        # 解析持仓表格 — 只取第一期（最新）的数据
        # 寻找第一个 <tbody> 中的行
        first_table = re.search(r"<tbody>(.*?)</tbody>", html, re.DOTALL)
        if not first_table:
            return report_date, []

        holdings = []
        row_pattern = re.compile(
            r"<tr>.*?"
            r"<td[^>]*>(\d+)</td>.*?"           # 序号
            r"<td[^>]*>.*?<a[^>]*>(.*?)</a>.*?</td>.*?"  # 股票代码
            r"<td[^>]*>.*?<a[^>]*>(.*?)</a>.*?</td>.*?"  # 股票名称
            r"<td[^>]*>([\d.]+)%</td>",          # 占净值比例
            re.DOTALL,
        )

        for m in row_pattern.finditer(first_table.group(1)):
            rank = int(m.group(1))
            stock_code = m.group(2).strip()
            stock_name = m.group(3).strip()
            hold_ratio = float(m.group(4))

            holdings.append({
                "stock_code": stock_code,
                "stock_name": stock_name,
                "hold_ratio": hold_ratio,
                "rank": rank,
            })

        if holdings and report_date:
            save_holdings(code, report_date, holdings)
            logger.debug("保存 %s 持仓 %d 只 (%s)", code, len(holdings), report_date)

        return report_date, holdings

    except Exception as e:
        logger.warning("抓取 %s 持仓失败: %s", code, str(e))
        return "", []


# ── 费率抓取 ─────────────────────────────────────────────


def fetch_fees(session, code):
    """
    从基金费率页面抓取管理费、托管费、申购费。
    返回: dict {management_fee, custody_fee, purchase_fee, total_cost}
    """
    url = FUND_FEE_URL.format(code=code)
    try:
        resp = session.get(url, headers=_random_headers(), timeout=20)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        html = resp.text

        result = {
            "management_fee": 0.0,
            "custody_fee": 0.0,
            "purchase_fee": 0.0,
        }

        # 管理费率
        mgmt_match = re.search(r"管理费率.*?([\d.]+)%", html)
        if mgmt_match:
            result["management_fee"] = float(mgmt_match.group(1))

        # 托管费率
        custody_match = re.search(r"托管费率.*?([\d.]+)%", html)
        if custody_match:
            result["custody_fee"] = float(custody_match.group(1))

        # 申购费率（取默认档 < 100万的费率）
        purchase_match = re.search(r"申购费率.*?([\d.]+)%", html)
        if purchase_match:
            result["purchase_fee"] = float(purchase_match.group(1))

        result["total_cost"] = round(
            result["management_fee"] + result["custody_fee"] + result["purchase_fee"], 2
        )

        return result

    except Exception as e:
        logger.warning("抓取 %s 费率失败: %s", code, str(e))
        return {"management_fee": 0.0, "custody_fee": 0.0, "purchase_fee": 0.0, "total_cost": 0.0}


# ── 最大回撤计算 ─────────────────────────────────────────


def calc_max_drawdown(nav_series):
    """
    从净值序列计算最大回撤。
    nav_series: list of dict {nav_date, nav} 或 list of float（正序：旧→新）

    最大回撤 = max((peak - trough) / peak) over all peaks
    返回: float (0~1, 例如 0.15 = 15% 回撤)
    """
    if not nav_series or len(nav_series) < 2:
        return 0.0

    navs = []
    for item in nav_series:
        if isinstance(item, dict):
            navs.append(item.get("nav", 0))
        else:
            navs.append(float(item))

    navs = [n for n in navs if n > 0]
    if len(navs) < 2:
        return 0.0

    peak = navs[0]
    max_dd = 0.0

    for nav in navs[1:]:
        if nav > peak:
            peak = nav
        else:
            dd = (peak - nav) / peak
            if dd > max_dd:
                max_dd = dd

    return round(max_dd, 4)


# ── Mag-7 占比计算 ───────────────────────────────────────


def calc_mag7_ratio(holdings):
    """
    计算持仓中 Mag-7 股票的总占比。
    holdings: list of {stock_code, hold_ratio}
    """
    if not holdings:
        return 0.0

    total = 0.0
    for h in holdings:
        ticker = h.get("stock_code", "").upper()
        if ticker in MAG7_TICKERS:
            total += h.get("hold_ratio", 0.0)

    return round(total, 2)


# ── 多周期收益计算 ───────────────────────────────────────


def calc_period_returns(nav_series):
    """
    从净值序列计算多周期收益率。
    nav_series: list of {nav_date, nav}（正序：旧→新）

    返回 dict: {week, month, quarter, half_year, year} — 各为小数
    """
    result = {
        "week_growth": 0.0,
        "month_growth": 0.0,
        "quarter_growth": 0.0,
        "half_year_growth": 0.0,
        "year_growth": 0.0,
    }

    if not nav_series or len(nav_series) < 2:
        return result

    latest_nav = nav_series[-1]["nav"]
    if latest_nav <= 0:
        return result

    # 将 nav_series 按日期索引
    nav_by_date = {item["nav_date"]: item["nav"] for item in nav_series}
    dates = sorted(nav_by_date.keys())

    today = dates[-1]
    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:
        return result

    period_map = {
        "week_growth": 7,
        "month_growth": 30,
        "quarter_growth": 90,
        "half_year_growth": 180,
        "year_growth": 365,
    }

    for key, days in period_map.items():
        target_dt = today_dt - timedelta(days=days)
        # 找最接近的日期的净值
        best_date = None
        best_diff = 999
        for d in dates:
            try:
                d_dt = datetime.strptime(d, "%Y-%m-%d")
                diff = abs((d_dt - target_dt).days)
                if diff < best_diff:
                    best_diff = diff
                    best_date = d
            except ValueError:
                continue

        if best_date and best_diff < 10:  # 容忍 10 天偏差
            old_nav = nav_by_date[best_date]
            if old_nav > 0:
                result[key] = round((latest_nav / old_nav) - 1, 6)

    return result


# ── 单只基金深度处理 ─────────────────────────────────────


def process_fund_deep(code):
    """
    对单只基金执行深度分析：
    1. 抓取 365 天历史净值
    2. 计算最大回撤 + 多周期涨幅
    3. 抓取持仓 → 计算 Mag-7 占比
    4. 抓取费率
    """
    session = _create_session()
    deep_data = {"deep_update": datetime.now().strftime("%Y-%m-%d %H:%M")}

    try:
        # 1. 历史净值
        nav_list = fetch_nav_history(session, code, days=365)
        _delay()

        # 获取既有 + 新增的完整净值（从 DB 读取）
        full_navs = get_nav_history(code, limit=365)

        # 2. 最大回撤
        if full_navs:
            deep_data["max_drawdown"] = calc_max_drawdown(full_navs)

            # 多周期涨幅
            period_returns = calc_period_returns(full_navs)
            deep_data.update(period_returns)

        # 3. 持仓
        report_date, holdings = fetch_holdings(session, code)
        _delay()

        if holdings:
            deep_data["mag7_ratio"] = calc_mag7_ratio(holdings)

        # 4. 费率
        fees = fetch_fees(session, code)
        _delay()
        deep_data.update(fees)

        # 写入数据库
        update_fund_deep_data(code, deep_data)
        return True

    except Exception as e:
        logger.error("深度分析 %s 失败: %s", code, str(e))
        return False


# ── 批量深度扫描 ─────────────────────────────────────────


def run_deep_scan():
    """
    对所有监控基金执行深度扫描。
    分批处理 + 并发 + 反爬延迟。
    """
    funds = get_all_funds(order_by_score=False)
    total = len(funds)

    if not funds:
        logger.warning("深度扫描：无基金数据")
        return 0, 0

    logger.info("=" * 50)
    logger.info("开始深度扫描: %d 只基金", total)

    success = 0
    failed = 0
    consecutive_failures = 0

    for batch_start in range(0, total, DEEP_SCAN_BATCH_SIZE):
        batch = funds[batch_start:batch_start + DEEP_SCAN_BATCH_SIZE]
        batch_num = batch_start // DEEP_SCAN_BATCH_SIZE + 1
        total_batches = (total + DEEP_SCAN_BATCH_SIZE - 1) // DEEP_SCAN_BATCH_SIZE

        logger.info("深度扫描批次 %d/%d (%d 只)", batch_num, total_batches, len(batch))

        with ThreadPoolExecutor(max_workers=DEEP_SCAN_WORKERS) as executor:
            future_to_code = {}
            for f in batch:
                future = executor.submit(process_fund_deep, f["code"])
                future_to_code[future] = f["code"]

            for future in as_completed(future_to_code):
                code = future_to_code[future]
                try:
                    ok = future.result()
                    if ok:
                        success += 1
                        consecutive_failures = 0
                    else:
                        failed += 1
                        consecutive_failures += 1
                except Exception as e:
                    logger.error("深度扫描 %s 异常: %s", code, str(e))
                    failed += 1
                    consecutive_failures += 1

                # 连续失败保护
                if consecutive_failures >= DEEP_SCAN_MAX_FAILURES:
                    logger.warning("连续 %d 次失败，暂停 5 分钟...", consecutive_failures)
                    time.sleep(300)
                    consecutive_failures = 0

        # 批次间休息
        if batch_start + DEEP_SCAN_BATCH_SIZE < total:
            logger.info("批次间休息 %d 秒...", DEEP_SCAN_BATCH_DELAY)
            time.sleep(DEEP_SCAN_BATCH_DELAY)

    logger.info("深度扫描完成: 成功 %d, 失败 %d", success, failed)
    logger.info("=" * 50)
    return success, failed
