"""
QDII Sentinel Pro 汇率模块
USD/CNY 实时抓取 + 趋势分析 + 汇率贡献度 + 风险评级
"""

import logging
from datetime import datetime, timedelta

import requests

from config import FX_API_URL
from models import save_exchange_rate, get_exchange_rates, get_latest_rate

logger = logging.getLogger(__name__)


def fetch_usd_cny():
    """
    从 ExchangeRate-API 获取最新 USD/CNY 和 USD/CNH 汇率并存入数据库。
    返回 dict {usd_cny, usd_cnh, rate_date} 或 None。
    """
    try:
        resp = requests.get(FX_API_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("result") != "success":
            logger.error("汇率 API 返回异常: %s", data)
            return None

        rates = data.get("rates", {})
        usd_cny = rates.get("CNY", 0.0)
        usd_cnh = rates.get("CNH", 0.0)

        # 使用 API 返回的更新时间作为日期
        rate_date = datetime.now().strftime("%Y-%m-%d")

        save_exchange_rate(rate_date, usd_cny, usd_cnh)

        logger.info("汇率更新: USD/CNY=%.4f, USD/CNH=%.4f", usd_cny, usd_cnh)
        return {
            "usd_cny": usd_cny,
            "usd_cnh": usd_cnh,
            "rate_date": rate_date,
        }

    except Exception as e:
        logger.error("汇率获取失败: %s", str(e))
        return None


def get_fx_trend(days=30):
    """
    获取最近 N 天的汇率走势。
    返回: list of {rate_date, usd_cny, usd_cnh}（旧→新）
    """
    rates = get_exchange_rates(days)
    return list(reversed(rates))  # 转为正序（旧→新）


def calc_fx_contribution(fund_year_return, fx_change_pct):
    """
    计算汇率贡献度。

    当人民币贬值（USD/CNY 上升）时，QDII 基金持有的美元资产以人民币计价会增值。
    汇率贡献度 = 净值中来自汇率变动的比例。

    参数:
        fund_year_return: 基金年度人民币计价收益率（小数，如 0.15 = 15%）
        fx_change_pct: 年度 USD/CNY 变动百分比（小数，如 0.03 = 人民币贬值 3%）

    返回:
        float — 汇率贡献占总收益的比例（0~1, 可能为负）
    """
    if abs(fund_year_return) < 0.0001:
        return 0.0

    # 汇率贡献度 ≈ 汇率变动% / 总收益%
    contribution = fx_change_pct / fund_year_return if fund_year_return != 0 else 0
    return round(contribution, 4)


def get_fx_risk_level():
    """
    根据近期汇率走势判定汇率风险等级。

    返回:
        dict {level: '高'/'中'/'低', trend: '升值'/'贬值'/'震荡',
              message: str, change_pct: float}
    """
    rates = get_exchange_rates(30)  # 最近30天（降序）
    if len(rates) < 2:
        return {
            "level": "未知",
            "trend": "数据不足",
            "message": "汇率数据不足，请等待系统积累更多数据",
            "change_pct": 0.0,
        }

    latest = rates[0]["usd_cny"]
    oldest = rates[-1]["usd_cny"]

    if oldest == 0:
        return {"level": "未知", "trend": "数据异常", "message": "", "change_pct": 0.0}

    # USD/CNY 变化：正值 = 美元升值 = 人民币贬值（对 QDII 有利）
    # 负值 = 美元贬值 = 人民币升值（对 QDII 不利）
    change_pct = (latest - oldest) / oldest

    if change_pct < -0.02:
        # 人民币升值 > 2%，高风险
        return {
            "level": "高",
            "trend": "人民币升值",
            "message": "⚠️ 当前汇率风险：高（人民币升值中，谨防净值折算缩水）",
            "change_pct": round(change_pct * 100, 2),
        }
    elif change_pct < -0.005:
        return {
            "level": "中",
            "trend": "人民币小幅升值",
            "message": "⚡ 当前汇率风险：中（人民币温和走强）",
            "change_pct": round(change_pct * 100, 2),
        }
    elif change_pct > 0.01:
        return {
            "level": "低",
            "trend": "人民币贬值",
            "message": "✅ 当前汇率风险：低（人民币走弱，利好 QDII 净值折算）",
            "change_pct": round(change_pct * 100, 2),
        }
    else:
        return {
            "level": "低",
            "trend": "震荡",
            "message": "✅ 当前汇率风险：低（汇率基本稳定）",
            "change_pct": round(change_pct * 100, 2),
        }


def get_fx_adjusted_return(fund_year_return, days=365):
    """
    计算汇率调整后的纯资产收益。

    QDII 基金的人民币计价收益 = 底层资产收益 + 汇率变动收益
    此函数剔除汇率变动部分，返回纯资产收益。

    注意: QDII 净值折算通常采用 T-1 日汇率。
    """
    rates = get_exchange_rates(days)
    if len(rates) < 2:
        return fund_year_return  # 无汇率数据，原样返回

    latest = rates[0]["usd_cny"]
    oldest = rates[-1]["usd_cny"]

    if oldest == 0:
        return fund_year_return

    fx_change = (latest - oldest) / oldest
    # 近似公式: 基金总收益 ≈ (1 + 资产收益) × (1 + 汇率变动) - 1
    # 因此: 资产收益 ≈ (1 + 总收益) / (1 + 汇率变动) - 1
    if abs(1 + fx_change) < 0.0001:
        return fund_year_return

    pure_return = (1 + fund_year_return) / (1 + fx_change) - 1
    return round(float(pure_return), 6)
