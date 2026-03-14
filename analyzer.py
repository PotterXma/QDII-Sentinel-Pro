"""
QDII Sentinel Pro 智能推荐引擎
加权评分 + 分类排名 + 智能标签 + 风险预警
"""

import re
import logging
from datetime import datetime

from config import SCORING_WEIGHTS, LIMIT_THRESHOLD_YUAN, MAG7_TICKERS, FUND_TYPE_RULES
from models import get_all_funds, update_fund_deep_data, get_holdings
from exchange_rate import get_fx_adjusted_return, get_fx_risk_level

logger = logging.getLogger(__name__)


# ── 基金分类 ─────────────────────────────────────────────


def classify_fund_type(name):
    """
    根据基金名称关键词自动识别基金类型。
    返回: str (如 '美股指数', '港股', '债券' 等)
    """
    for fund_type, keywords in FUND_TYPE_RULES.items():
        if any(kw in name for kw in keywords):
            return fund_type
    return "其他"


def detect_ac_pair(name, code):
    """
    检测 A/C 类份额配对组。
    返回: str — 配对组标识（相同名称主体的 A/C 类基金会有相同的组标识）
    """
    # 类似 "华夏全球科技先锋混合(QDII)A" → 提取 "华夏全球科技先锋混合(QDII)"
    cleaned = re.sub(r'[AC](\(.*?\))?$', '', name).strip()
    cleaned = re.sub(r'(人民币|美元)(现汇|现钞)?$', '', cleaned).strip()
    return cleaned if cleaned != name else ""


# ── 评分维度 ─────────────────────────────────────────────


def _score_limit(fund):
    """
    限额可用性评分 (0~100)
    limit_yuan < 500 → 0 (无法有效建仓)
    500~10000 → 20~60
    10000~100000 → 60~90
    100000+ → 90~100
    """
    limit = fund.get("limit_yuan", -1)

    # 特殊值处理
    if limit == 0:
        return 0  # 暂停申购
    if limit == -1 or limit == -2:
        return 10  # 未知或暂停大额
    if limit >= 999999999:
        return 100  # 不限额

    if limit < LIMIT_THRESHOLD_YUAN:
        return 0

    if limit < 10000:
        return 20 + (limit - LIMIT_THRESHOLD_YUAN) / (10000 - LIMIT_THRESHOLD_YUAN) * 40
    elif limit < 100000:
        return 60 + (limit - 10000) / (100000 - 10000) * 30
    else:
        return min(90 + (limit - 100000) / 1000000 * 10, 100)


def _score_drawdown(fund, type_funds):
    """
    回撤控制力评分 (0~100)
    在同类基金中，最大回撤越小，评分越高。
    """
    dd = fund.get("max_drawdown", 0)
    if dd == 0:
        return 50  # 无数据，给中位分

    # 收集同类基金的回撤数据
    dds = [f.get("max_drawdown", 0) for f in type_funds if f.get("max_drawdown", 0) > 0]
    if not dds:
        return 50

    max_dd = max(dds)
    min_dd = min(dds)

    if max_dd == min_dd:
        return 70

    # 线性映射: 最小回撤 → 100, 最大回撤 → 20
    score = 100 - (dd - min_dd) / (max_dd - min_dd) * 80
    return max(0, min(100, score))


def _score_fx_return(fund):
    """
    汇率调整后收益评分 (0~100)
    年度收益剔除汇率影响后，越高越好。
    """
    year_return = fund.get("year_growth", 0)
    if year_return == 0:
        return 30  # 无数据

    # 剔除汇率影响
    pure_return = get_fx_adjusted_return(year_return)

    # 映射: -30% → 0, 0% → 40, +30% → 100
    score = 40 + pure_return * 200
    return max(0, min(100, score))


def _score_asset_quality(fund):
    """
    底层资产质量评分 (0~100)
    - 股票型基金: Mag-7 占比在 35-55% 之间最优
    - 非股票型: 使用年涨幅稳定性（给中位分）
    """
    fund_type = fund.get("fund_type", "")

    # 非股票类基金（债券、商品等）无 Mag-7 概念
    if fund_type in ("债券", "商品", "房地产"):
        # 以年涨幅作为替代指标
        return min(100, max(0, 50 + fund.get("year_growth", 0) * 200))

    mag7 = fund.get("mag7_ratio", 0)
    if mag7 == 0:
        return 40  # 无数据或非美股基金

    # 35-55% 最优，偏离越多扣分
    if 35 <= mag7 <= 55:
        return 90 + (45 - abs(mag7 - 45)) / 10 * 10  # 45% 附近得 95-100
    elif 25 <= mag7 < 35 or 55 < mag7 <= 65:
        return 60 + (10 - abs(mag7 - 45) + 10) / 20 * 30
    elif mag7 > 65:
        return max(20, 60 - (mag7 - 65) * 2)  # 过于集中，风险大
    else:
        return max(20, 40 + mag7)  # 占比很低


def _score_cost(fund):
    """
    成本优化评分 (0~100)
    年度综合持有成本越低越好。
    """
    total = fund.get("total_cost", 0)
    if total == 0:
        return 50  # 无数据

    # 映射: 0% → 100, 1% → 70, 2% → 40, 3%+ → 10
    score = 100 - total * 30
    return max(10, min(100, score))


# ── 智能标签 ─────────────────────────────────────────────


def assign_tags(fund, all_funds):
    """
    为基金分配智能标签。
    返回: str (逗号分隔的标签)
    """
    tags = []
    limit = fund.get("limit_yuan", -1)
    dd = fund.get("max_drawdown", 0)
    mag7 = fund.get("mag7_ratio", 0)
    year_g = fund.get("year_growth", 0)

    # 大额友好：限额 > 100,000 元且回撤 < 15%
    if limit >= 100000 and dd < 0.15 and dd > 0:
        tags.append("大额友好")
    elif limit >= 999999999:
        tags.append("大额友好")

    # 极致进攻：Mag-7 占比 > 50% 且年涨幅领先
    year_growths = sorted(
        [f.get("year_growth", 0) for f in all_funds if f.get("year_growth", 0) > 0],
        reverse=True,
    )
    top_20_threshold = year_growths[len(year_growths) // 5] if len(year_growths) > 5 else 0.1
    if mag7 > 50 and year_g >= top_20_threshold:
        tags.append("极致进攻")

    # 防御稳健：指数类 + 回撤极小
    fund_type = fund.get("fund_type", "")
    if fund_type in ("美股指数",) and dd < 0.10 and dd > 0:
        tags.append("防御稳健")

    # 低成本：年度总费用 < 1%
    if fund.get("total_cost", 0) > 0 and fund.get("total_cost", 0) < 1.0:
        tags.append("低成本")

    return ",".join(tags)


# ── 风险预警 ─────────────────────────────────────────────


def check_risk_warnings(fund):
    """
    检查风险警示信号。
    返回: str (逗号分隔的警示)
    """
    warnings = []

    # 暂停申购
    if fund.get("limit_amount", 0) == 0:
        warnings.append("暂停申购")

    # 近期回撤异常 (> 25%)
    dd = fund.get("max_drawdown", 0)
    if dd > 0.25:
        warnings.append(f"回撤异常({dd*100:.1f}%)")

    # 季度亏损 > 15%
    q_growth = fund.get("quarter_growth", 0)
    if q_growth < -0.15:
        warnings.append(f"季度大跌({q_growth*100:.1f}%)")

    # 费用过高 > 3%
    cost = fund.get("total_cost", 0)
    if cost > 3.0:
        warnings.append(f"费率过高({cost:.1f}%)")

    return ",".join(warnings)


# ── 主评分引擎 ───────────────────────────────────────────


def analyze_fund_investment_value():
    """
    对所有监控基金进行加权打分。

    评分逻辑:
    1. 限额可用性 (30%): < 500 元直接剔除
    2. 回撤控制力 (20%): 同类比较
    3. 汇率调整后收益 (20%): 剔除 FX 影响
    4. 底层资产质量 (20%): Mag-7 占比 35-55% 最优
    5. 成本优化 (10%): 综合费用越低越好

    写入数据库后返回 TOP 推荐列表。
    """
    funds = get_all_funds(order_by_score=False)
    if not funds:
        return []

    logger.info("开始评分分析: %d 只基金", len(funds))

    # 按类型分组（用于同类比较）
    type_groups = {}
    for f in funds:
        ft = f.get("fund_type", "其他")
        if ft not in type_groups:
            type_groups[ft] = []
        type_groups[ft].append(f)

    scored_funds = []

    for f in funds:
        ft = f.get("fund_type", "其他")
        type_funds = type_groups.get(ft, [f])

        # 各维度打分
        s_limit = _score_limit(f)
        s_drawdown = _score_drawdown(f, type_funds)
        s_fx_return = _score_fx_return(f)
        s_asset = _score_asset_quality(f)
        s_cost = _score_cost(f)

        # 加权总分
        w = SCORING_WEIGHTS
        total_score = (
            s_limit * w["limit"]
            + s_drawdown * w["drawdown"]
            + s_fx_return * w["fx_return"]
            + s_asset * w["asset_quality"]
            + s_cost * w["cost"]
        )

        # 限额过低直接清零
        if s_limit == 0:
            total_score = 0

        total_score = round(total_score, 1)

        # 标签 & 警告
        tags = assign_tags(f, funds)
        warnings = check_risk_warnings(f)

        # A/C 配对
        pair_group = detect_ac_pair(f.get("name", ""), f.get("code", ""))

        # 写入数据库
        update_fund_deep_data(f["code"], {
            "score": total_score,
            "tags": tags,
            "risk_warning": warnings,
            "pair_group": pair_group,
            "fund_type": ft,
        })

        scored_funds.append({
            **f,
            "score": total_score,
            "tags": tags,
            "risk_warning": warnings,
        })

    # 按分数排序
    scored_funds.sort(key=lambda x: x["score"], reverse=True)

    logger.info("评分完成: TOP-5 = %s",
                [(f["code"], f["name"][:10], f["score"]) for f in scored_funds[:5]])

    return scored_funds


def get_top_recommendations(n=5):
    """获取评分最高的 N 只基金"""
    funds = get_all_funds(order_by_score=True)
    # 过滤掉评分为 0 的
    ranked = [f for f in funds if f.get("score", 0) > 0]
    return ranked[:n]
