"""
QDII Watcher 汇率追踪模块
定时获取 USD/CNY 汇率，存入数据库
"""

import logging
import requests

from config import FX_API_URL
from models import save_exchange_rate, get_latest_rate, get_rate_change

logger = logging.getLogger(__name__)


def fetch_usd_cny_rate():
    """
    从 open.er-api.com 获取 USD→CNY 汇率
    返回: float (汇率值) 或 None (失败)
    """
    try:
        resp = requests.get(FX_API_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("result") == "success":
            rates = data.get("rates", {})
            cny = rates.get("CNY")
            if cny:
                logger.info("获取汇率成功: USD/CNY = %.4f", cny)
                return float(cny)

        logger.warning("汇率 API 返回异常: %s", data.get("result"))
        return None

    except Exception as e:
        logger.error("获取汇率失败: %s", str(e))
        return None


def update_exchange_rate():
    """获取最新汇率并保存到数据库"""
    rate = fetch_usd_cny_rate()
    if rate is not None:
        save_exchange_rate(rate)
        return rate

    logger.warning("汇率更新失败，使用上次记录")
    last = get_latest_rate()
    return last["rate"] if last else None


def get_fx_summary():
    """获取汇率摘要信息（用于 UI 展示）"""
    latest = get_latest_rate()
    change_30d = get_rate_change(days=30)

    return {
        "current_rate": latest["rate"] if latest else None,
        "last_updated": latest["recorded_at"] if latest else None,
        "change_30d": change_30d,
        "change_30d_pct": f"{change_30d * 100:+.2f}%" if change_30d else "N/A",
    }
