"""
QDII Watcher 爬虫模块
明确 API 端点 / 重试机制 / 随机延迟 / 容错隔离 / 扩展限额解析
支持 MONITOR_ALL_QDII 模式
"""

import re
import json
import time
import random
import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    FUND_LIST_URL,
    FUND_DETAIL_URL,
    USER_AGENTS,
    US_KEYWORDS,
    MONITOR_ALL_QDII,
)

logger = logging.getLogger(__name__)


# ── HTTP 工具 ────────────────────────────────────────────


def get_random_headers():
    """随机 User-Agent + Referer"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": "http://fund.eastmoney.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def create_session():
    """创建带重试策略的 requests.Session"""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# ── HTML 清理 ────────────────────────────────────────────


def _strip_html(text):
    """移除 HTML 标签，只保留纯文本内容"""
    if not text:
        return text
    cleaned = re.sub(r'<[^>]+>', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


# ── 限额解析 ─────────────────────────────────────────────


def parse_limit(text):
    """
    将限额文本解析为万元数值。

    返回值约定:
        999999.0  —  不限 / 无限制
         50.0     —  50 万元
          0.1     —  1000 元 (= 0.1 万)
          0.0     —  暂停申购
         -1.0     —  无法识别（兜底）
         -2.0     —  暂停大额申购（部分暂停）
    """
    if not text or not isinstance(text, str):
        return -1.0

    text = text.strip()

    if "暂停大额" in text:
        return -2.0
    if "暂停" in text:
        return 0.0

    if text in ("不限", "无限制") or "不限" in text or "无限制" in text:
        return 999999.0

    if text == "开放申购" or text == "开放":
        return 999999.0

    match = re.search(r"([\d,]+(?:\.\d+)?)", text)
    if match:
        num_str = match.group(1).replace(",", "")
        try:
            num = float(num_str)
        except ValueError:
            return -1.0
        if "万" in text:
            return num
        else:
            return num / 10000

    return -1.0


# ── QDII 基金列表 ────────────────────────────────────────


def fetch_qdii_list(session):
    """
    从天天基金 fundcode_search.js 获取 QDII 基金列表。
    返回: list of (code, name)
    """
    logger.info("正在获取基金列表: %s", FUND_LIST_URL)
    try:
        resp = session.get(FUND_LIST_URL, headers=get_random_headers(), timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8"  # 防止 GBK 导致基金名乱码
        content = resp.text

        match = re.search(r"\[.*\]", content, re.DOTALL)
        if not match:
            logger.error("无法从 fundcode_search.js 解析基金列表")
            return []

        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            logger.error("基金列表 JSON 解析失败: %s", str(e))
            return []

        qdii_funds = []
        for item in data:
            if len(item) >= 4 and str(item[3]).upper().startswith("QDII"):
                qdii_funds.append((item[0], item[2]))

        logger.info("共获取 %d 只 QDII 基金", len(qdii_funds))
        return qdii_funds

    except Exception as e:
        logger.error("获取基金列表失败: %s", str(e))
        return []


def filter_us_funds(qdii_funds):
    """关键词二次过滤：只保留美股相关的 QDII 基金。"""
    result = []
    for code, name in qdii_funds:
        if any(kw in name for kw in US_KEYWORDS):
            result.append((code, name))
    logger.info("关键词过滤后剩余 %d 只美股 QDII 基金", len(result))
    return result


# ── 单只基金详情 ──────────────────────────────────────────


def fetch_fund_detail(session, code):
    """抓取单只基金的申购状态和净值信息。"""
    url = FUND_DETAIL_URL.format(code=code)
    try:
        resp = session.get(url, headers=get_random_headers(), timeout=20)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        html = resp.text

        result = {"code": code}

        name_match = re.search(r'<span class="funCur-FundName">(.*?)</span>', html)
        if name_match:
            result["name"] = name_match.group(1).strip()

        nav_match = re.search(r'<span class="NAV[^"]*?">([\d.]+)</span>', html, re.IGNORECASE)
        if not nav_match:
            nav_match = re.search(r'class="dataNums"[^>]*?>\s*<span[^>]*?>([\d.]+)', html)
        if nav_match:
            try:
                result["current_nav"] = float(nav_match.group(1))
            except ValueError:
                result["current_nav"] = 0.0
        else:
            result["current_nav"] = 0.0

        growth_match = re.search(
            r'class="(?:nav|dataNums)[^"]*?"[^>]*>.*?<span[^>]*?>([-+]?[\d.]+)%',
            html, re.DOTALL
        )
        if growth_match:
            try:
                result["day_growth"] = float(growth_match.group(1)) / 100
            except ValueError:
                result["day_growth"] = 0.0
        else:
            result["day_growth"] = 0.0

        limit_text = ""
        status_match = re.search(
            r'(?:申购状态|交易状态|申购)[：:]\s*<[^>]*>(.*?)</[^>]*>', html
        )
        if status_match:
            limit_text = status_match.group(1).strip()

        if not limit_text:
            limit_match = re.search(
                r'(?:限大额|限额|申购限额)[^<]*?(?:<[^>]*>)?\s*([\d,]+(?:\.\d+)?(?:万)?元)',
                html,
            )
            if limit_match:
                limit_text = limit_match.group(0).strip()

        if not limit_text:
            buy_match = re.search(
                r'class="[^"]*?fundBuy[^"]*?"[^>]*>.*?(?:暂停|开放|限大?额)(.*?)<',
                html, re.DOTALL,
            )
            if buy_match:
                limit_text = buy_match.group(0).strip()
                limit_text = re.sub(r'<[^>]+>', '', limit_text).strip()

        limit_text = _strip_html(limit_text) or "未知"
        result["limit_text"] = limit_text
        result["limit_amount"] = parse_limit(limit_text)

        from datetime import datetime
        result["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        return result

    except Exception as e:
        logger.warning("抓取基金 %s 详情失败: %s", code, str(e))
        return None


# ── 完整扫描 ─────────────────────────────────────────────


def run_full_scan():
    """
    完整流程：获取列表 → 过滤 → 逐只抓取 → 写入 → 通知
    根据 MONITOR_ALL_QDII 决定是否跳过关键词过滤。
    """
    from models import init_db, upsert_fund
    from notifier import notify_all

    logger.info("=" * 50)
    logger.info("开始完整扫描 (monitor_all_qdii=%s)...", MONITOR_ALL_QDII)

    init_db()
    session = create_session()

    # 1. 获取 QDII 列表
    qdii_funds = fetch_qdii_list(session)
    if not qdii_funds:
        logger.error("未获取到任何 QDII 基金，扫描终止")
        return 0, 0, []

    # 2. 是否关键词过滤
    if MONITOR_ALL_QDII:
        target_funds = qdii_funds
        logger.info("全量 QDII 模式: 监控 %d 只基金", len(target_funds))
    else:
        target_funds = filter_us_funds(qdii_funds)
        if not target_funds:
            logger.warning("关键词过滤后无匹配基金")
            return 0, 0, []

    # 3. 逐只抓取详情
    success = 0
    failed = 0
    changes = []

    for i, (code, name) in enumerate(target_funds, 1):
        logger.info("[%d/%d] 正在抓取: %s %s", i, len(target_funds), code, name)

        try:
            detail = fetch_fund_detail(session, code)
            if detail is None:
                failed += 1
                continue

            if "name" not in detail or not detail["name"]:
                detail["name"] = name

            change = upsert_fund(detail)
            if change:
                changes.append(change)

            success += 1

        except Exception as e:
            logger.error("处理基金 %s 异常: %s", code, str(e))
            failed += 1

        delay = random.uniform(1.0, 3.0)
        time.sleep(delay)

    logger.info("扫描完成: 成功 %d, 失败 %d, 变动 %d", success, failed, len(changes))

    # 4. 通过所有通道发送通知
    if changes:
        notify_all(changes)

    logger.info("=" * 50)
    return success, failed, changes
