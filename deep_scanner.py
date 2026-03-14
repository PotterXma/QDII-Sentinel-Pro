"""
QDII Watcher 深度扫描模块
批量获取: 历史净值 / 持仓 / 费率 / 基金经理 / 规模
使用线程池 + 批次控制 + 失败阈值
"""

import re
import json
import time
import random
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    USER_AGENTS,
    DS_BATCH_SIZE,
    DS_BATCH_DELAY,
    DS_REQUEST_DELAY_MIN,
    DS_REQUEST_DELAY_MAX,
    DS_MAX_FAILURES,
    DS_WORKERS,
)
from models import (
    get_all_funds,
    upsert_fund_detail,
    get_fund_detail,
)

logger = logging.getLogger(__name__)


def _get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": "http://fund.eastmoney.com/",
    }


def _create_session():
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# ── 数据抓取函数 ─────────────────────────────────────────


def fetch_fund_info(session, code):
    """
    从天天基金详情页抓取:
    - 基金经理
    - 基金规模（亿元）
    - 管理费率 + 托管费率
    - 前十大持仓（名称列表）
    """
    url = f"http://fund.eastmoney.com/{code}.html"
    result = {"code": code}

    try:
        resp = session.get(url, headers=_get_headers(), timeout=15)
        resp.raise_for_status()
        html = resp.content.decode('utf-8', 'ignore')

        # 基金经理
        mgr_match = re.search(r'基金经理.*?<a[^>]*>(.*?)</a>', html, re.DOTALL)
        result["manager"] = mgr_match.group(1).strip() if mgr_match else ""

        # 基金规模（亿元）
        size_match = re.search(r'规模[^>]*?[：:]\s*([\d.]+)\s*亿', html, re.DOTALL)
        result["fund_size"] = float(size_match.group(1)) if size_match else 0.0

        # 管理费率
        fee_match = re.search(r'管理费率.*?([\d.]+)%', html, re.DOTALL)
        custody_match = re.search(r'托管费率.*?([\d.]+)%', html, re.DOTALL)
        fee = float(fee_match.group(1)) if fee_match else 0.0
        custody = float(custody_match.group(1)) if custody_match else 0.0
        result["fee_rate"] = fee + custody  # 总费率（%）

    except Exception as e:
        logger.warning("获取基金 %s 基本信息失败: %s", code, str(e))

    return result


def fetch_nav_history(session, code, days=100):
    """
    获取历史净值（近 N 天），用于计算最大回撤和多区间涨幅。
    天天基金 API 每页最多返回 20 条，需要分页请求。
    """
    PAGE_SIZE = 20
    pages_needed = (days + PAGE_SIZE - 1) // PAGE_SIZE  # 向上取整

    all_nav = []

    for page in range(1, pages_needed + 1):
        url = (
            "http://api.fund.eastmoney.com/f10/lsjz"
            f"?fundCode={code}&pageIndex={page}&pageSize={PAGE_SIZE}"
        )
        headers = _get_headers()
        headers["Referer"] = f"http://fundf10.eastmoney.com/jjjz_{code}.html"

        try:
            resp = session.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            data_dict = data.get("Data") or {}
            items = data_dict.get("LSJZList") or []
            if not items:
                break  # 没有更多数据

            for item in items:
                try:
                    nav = float(item.get("DWJZ", 0))
                    date = item.get("FSRQ", "")
                    if nav > 0:
                        all_nav.append({"date": date, "nav": nav})
                except (ValueError, TypeError):
                    continue

            if len(items) < PAGE_SIZE:
                break  # 最后一页

            # 页间小延迟，避免被封
            time.sleep(random.uniform(0.3, 0.8))

        except Exception as e:
            logger.warning("获取基金 %s 历史净值第%d页失败: %s", code, page, str(e))
            break

    return all_nav


def calc_max_drawdown(nav_list):
    """
    计算最大回撤（百分比，正数）。
    nav_list: [{"date": "2025-01-01", "nav": 1.234}, ...]  按时间正序
    """
    if len(nav_list) < 2:
        return 0.0

    # 确保按日期正序
    sorted_navs = sorted(nav_list, key=lambda x: x["date"])
    values = [item["nav"] for item in sorted_navs]

    max_drawdown = 0.0
    peak = values[0]

    for v in values[1:]:
        if v > peak:
            peak = v
        drawdown = (peak - v) / peak
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    return max_drawdown


# ── 单只基金深度扫描 ─────────────────────────────────────


def deep_scan_single(code):
    """对单只基金执行深度扫描"""
    session = _create_session()
    result = {"code": code}

    try:
        # 获取基本信息（经理/规模/费率）
        info = fetch_fund_info(session, code)
        result.update(info)

        # 延迟
        time.sleep(random.uniform(DS_REQUEST_DELAY_MIN, DS_REQUEST_DELAY_MAX))

        # 获取历史净值
        nav_list = fetch_nav_history(session, code)
        if nav_list:
            result["max_drawdown"] = calc_max_drawdown(nav_list)
            # 保存最近 100 天净值用于计算多区间涨幅（近三月需要约90天数据）
            recent_navs = sorted(nav_list, key=lambda x: x["date"], reverse=True)[:100]
            result["nav_history"] = json.dumps(recent_navs, ensure_ascii=False)
        else:
            result["max_drawdown"] = 0.0
            result["nav_history"] = "[]"

        # 写入数据库
        upsert_fund_detail(result)
        return True

    except Exception as e:
        logger.error("深度扫描基金 %s 异常: %s", code, str(e))
        return False

    finally:
        session.close()


# ── 批量深度扫描 ─────────────────────────────────────────


def run_deep_scan():
    """
    批量深度扫描所有已监控的基金。
    按批次执行，使用线程池，连续失败达阈值则中止。
    """
    funds = get_all_funds(order_by_limit=False)
    if not funds:
        logger.warning("无基金数据，跳过深度扫描")
        return

    codes = [f["code"] for f in funds]
    total = len(codes)
    logger.info("=" * 50)
    logger.info("开始深度扫描: %d 只基金, 批次大小=%d, 线程数=%d",
                total, DS_BATCH_SIZE, DS_WORKERS)

    success_total = 0
    fail_total = 0

    # 分批
    for batch_start in range(0, total, DS_BATCH_SIZE):
        batch = codes[batch_start: batch_start + DS_BATCH_SIZE]
        batch_num = batch_start // DS_BATCH_SIZE + 1
        logger.info("批次 %d: 处理 %d 只基金 (%d/%d)",
                     batch_num, len(batch), batch_start + 1, total)

        consecutive_failures = 0
        batch_success = 0
        batch_fail = 0

        with ThreadPoolExecutor(max_workers=DS_WORKERS) as executor:
            future_to_code = {
                executor.submit(deep_scan_single, code): code
                for code in batch
            }

            for future in as_completed(future_to_code):
                code = future_to_code[future]
                try:
                    if future.result():
                        batch_success += 1
                        consecutive_failures = 0
                    else:
                        batch_fail += 1
                        consecutive_failures += 1
                except Exception as e:
                    batch_fail += 1
                    consecutive_failures += 1
                    logger.error("基金 %s 深度扫描异常: %s", code, str(e))

                if consecutive_failures >= DS_MAX_FAILURES:
                    logger.error(
                        "连续失败 %d 次，中止当前批次", consecutive_failures
                    )
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

        success_total += batch_success
        fail_total += batch_fail

        logger.info("批次 %d 完成: 成功=%d, 失败=%d",
                     batch_num, batch_success, batch_fail)

        # 批次间休息
        if batch_start + DS_BATCH_SIZE < total:
            logger.info("批次间休息 %d 秒...", DS_BATCH_DELAY)
            time.sleep(DS_BATCH_DELAY)

    logger.info("深度扫描全部完成: 成功=%d, 失败=%d", success_total, fail_total)
    logger.info("=" * 50)
    return success_total, fail_total
