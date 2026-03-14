"""
QDII Watcher 基金评分模块
五维加权评分: 限额 / 回撤 / 汇率收益 / 资产质量 / 费用
"""

import logging

from config import (
    SCORE_WEIGHT_LIMIT,
    SCORE_WEIGHT_DRAWDOWN,
    SCORE_WEIGHT_FX_RETURN,
    SCORE_WEIGHT_ASSET_QUALITY,
    SCORE_WEIGHT_COST,
    LIMIT_THRESHOLD_YUAN,
)
from models import (
    get_all_funds,
    get_fund_detail,
    upsert_fund_detail,
    get_rate_change,
)

logger = logging.getLogger(__name__)


def _score_limit(limit_amount):
    """
    限额评分 (0~100)
    不限/999999 → 100
    >1万 → 80
    >1000元(0.1万) → 50
    >500元(0.05万) → 20
    ≤500元 → 0
    暂停(0.0) → 0
    未知(-1) → 30 (给予中性分)
    暂停大额(-2) → 10
    """
    if limit_amount >= 999999:
        return 100
    if limit_amount > 1.0:
        return 80
    if limit_amount > 0.1:
        return 60
    if limit_amount > LIMIT_THRESHOLD_YUAN / 10000:
        return 40
    if limit_amount > 0:
        return 20
    if limit_amount == 0.0:
        return 0
    if limit_amount == -2.0:
        return 10
    # -1.0 (未知)
    return 30


def _score_drawdown(max_drawdown):
    """
    最大回撤评分 (0~100)
    回撤越小越好，线性映射:
    0% → 100, 50%+ → 0
    """
    if max_drawdown <= 0:
        return 100
    if max_drawdown >= 0.5:
        return 0
    return max(0, 100 - max_drawdown * 200)


def _score_fx_return(fx_change_30d):
    """
    汇率收益评分 (0~100)
    美元升值对 QDII 有利。
    +3%以上 → 100
    0% → 50
    -3%以下 → 0
    """
    if fx_change_30d is None:
        return 50  # 无数据给中性分
    # 映射 [-3%, +3%] → [0, 100]
    score = 50 + (fx_change_30d * 100) * (50 / 3)
    return max(0, min(100, score))


def _score_asset_quality(fund_size, top_holdings_count=0):
    """
    资产质量评分 (0~100)
    基于基金规模:
    >50亿 → 100, >10亿 → 80, >1亿 → 60, >0.1亿 → 40, ≤0.1亿 → 20
    """
    if fund_size >= 50:
        return 100
    if fund_size >= 10:
        return 80
    if fund_size >= 1:
        return 60
    if fund_size >= 0.1:
        return 40
    return 20


def _score_cost(fee_rate):
    """
    费用评分 (0~100)
    总费率(管理+托管)越低越好:
    ≤0.5% → 100, ≤1.0% → 80, ≤1.5% → 60, ≤2.0% → 40, >2.0% → 20
    """
    if fee_rate <= 0:
        return 50  # 无数据给中性分
    if fee_rate <= 0.5:
        return 100
    if fee_rate <= 1.0:
        return 80
    if fee_rate <= 1.5:
        return 60
    if fee_rate <= 2.0:
        return 40
    return 20


def calc_score(fund, detail, fx_change_30d=None):
    """
    计算单只基金的综合评分。

    fund: dict (来自 funds 表)
    detail: dict (来自 fund_detail 表) 或 None
    fx_change_30d: float (30 天汇率变动百分比) 或 None

    返回: float 0~100
    """
    limit_score = _score_limit(fund.get("limit_amount", -1))

    dd = detail.get("max_drawdown", 0) if detail else 0
    drawdown_score = _score_drawdown(dd)

    fx_score = _score_fx_return(fx_change_30d)

    size = detail.get("fund_size", 0) if detail else 0
    quality_score = _score_asset_quality(size)

    fee = detail.get("fee_rate", 0) if detail else 0
    cost_score = _score_cost(fee)

    total = (
        limit_score * SCORE_WEIGHT_LIMIT
        + drawdown_score * SCORE_WEIGHT_DRAWDOWN
        + fx_score * SCORE_WEIGHT_FX_RETURN
        + quality_score * SCORE_WEIGHT_ASSET_QUALITY
        + cost_score * SCORE_WEIGHT_COST
    )

    return round(total, 1)


def update_all_scores():
    """
    重新计算所有基金的评分并写入数据库。
    通常在深度扫描或汇率更新后调用。
    """
    funds = get_all_funds(order_by_limit=False)
    fx_change = get_rate_change(days=30)

    updated = 0
    for fund in funds:
        code = fund["code"]
        detail = get_fund_detail(code)

        score = calc_score(fund, detail, fx_change)

        # 更新评分
        detail_data = {
            "code": code,
            "manager": detail.get("manager", "") if detail else "",
            "fund_size": detail.get("fund_size", 0) if detail else 0,
            "fee_rate": detail.get("fee_rate", 0) if detail else 0,
            "max_drawdown": detail.get("max_drawdown", 0) if detail else 0,
            "top_holdings": detail.get("top_holdings", "") if detail else "",
            "nav_history": detail.get("nav_history", "[]") if detail else "[]",
            "score": score,
        }
        upsert_fund_detail(detail_data)
        updated += 1

    logger.info("评分更新完成: %d 只基金", updated)
    return updated


# ── 基金分类 ─────────────────────────────────────────────

# 关键词 → 类型映射（优先匹配靠前的规则）
_FUND_TYPE_RULES = [
    ("债券", ["债", "固收", "利率", "信用"]),
    ("股票-美股", ["纳斯达克", "纳指", "标普", "美国", "道琼斯", "费城半导体",
                  "罗素", "美股", "NASDAQ", "S&P"]),
    ("股票-港股", ["恒生", "港股", "香港", "中概", "H股"]),
    ("股票-全球", ["全球", "亚太", "新兴", "欧洲", "日本", "印度", "越南"]),
    ("商品", ["黄金", "原油", "石油", "商品", "能源", "有色"]),
    ("房地产", ["REITs", "房地产", "不动产"]),
]


def classify_fund_type(name):
    """
    根据基金名称关键词自动分类。
    返回: str (如 '股票-美股', '债券', '商品')
    """
    for fund_type, keywords in _FUND_TYPE_RULES:
        if any(kw in name for kw in keywords):
            return fund_type
    return "混合/其他"


def get_top5_recommendations():
    """
    获取评分最高的 TOP5 基金，附带类型标签。
    返回: list of dict，每个包含 code/name/score/fund_type/limit_amount/fund_size 等
    """
    from models import get_funds_with_details

    funds = get_funds_with_details(order_by="score")

    # 过滤掉暂停申购和评分为 0 的
    active = [f for f in funds if f.get("score", 0) > 0 and f.get("limit_amount", 0) != 0.0]

    top5 = []
    for f in active[:5]:
        top5.append({
            "code": f["code"],
            "name": f["name"],
            "score": f.get("score", 0),
            "fund_type": classify_fund_type(f["name"]),
            "limit_amount": f.get("limit_amount", -1),
            "fund_size": f.get("fund_size", 0),
            "fee_rate": f.get("fee_rate", 0),
            "current_nav": f.get("current_nav", 0),
            "day_growth": f.get("day_growth", 0),
            "max_drawdown": f.get("max_drawdown", 0),
        })

    return top5

