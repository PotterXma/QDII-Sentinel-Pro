"""
QDII 哨兵 Pro 通知模块
三通道推送: Bark (iOS) / 企业微信 Webhook / SMTP 邮件
+ Bark URL 编码 (urllib.parse.quote)
+ push_log 幂等推送 (防止重复报警)
"""

import smtplib
import logging
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

from config import (
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_RECEIVER,
    BARK_KEY, BARK_SERVER, BARK_SOUND, BARK_LEVEL,
    WECHAT_WEBHOOK_URL,
    PUSH_DEDUP_ENABLED,
)

logger = logging.getLogger(__name__)


# ── 限额状态判定 ─────────────────────────────────────────


def _limit_status(limit_amount):
    """将限额值映射为推送去重用的状态字符串"""
    if limit_amount is None:
        return "new"
    if limit_amount == 0.0:
        return "paused"
    if limit_amount == -1.0:
        return "unknown"
    if limit_amount == -2.0:
        return "large_paused"
    if limit_amount == -3.0:
        return "not_for_sale"
    if limit_amount >= 999999.0:
        return "unlimited"
    return f"limited_{limit_amount:.2f}"


def _format_limit(val):
    """格式化限额显示（统一为：暂停申购、未限购、xxx元）"""
    if val is None:
        return "新增"
    if val == 0.0:
        return "暂停申购"
    if val == -1.0:
        return "未知状态"
    if val == -2.0:
        return "暂停大额"
    if val == -3.0:
        return "暂不销售"
    if val >= 999999.0:
        return "未限购"
    
    yuan = int(round(val * 10000))
    return f"{yuan:,}元"


# ── Bark 推送（iOS） ─────────────────────────────────────


def send_bark_push(title, body, group="QDII", url=None):
    """
    通过 Bark 发送 iOS 推送通知。
    点击通知可跳转 url（默认打开本地看板）。
    """
    if not BARK_KEY:
        logger.debug("Bark 未配置，跳过推送")
        return False

    try:
        url_api = f"{BARK_SERVER}/{BARK_KEY}"
        payload = {
            "title": title,
            "body": body,
            "group": group,
            "sound": BARK_SOUND,
            "level": BARK_LEVEL,
            "isArchive": 1,
        }
        if url:
            payload["url"] = url
        resp = requests.post(url_api, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 200:
            logger.info("Bark 推送成功: %s", title)
            return True
        else:
            logger.warning("Bark 推送返回异常: %s", data)
            return False
    except Exception as e:
        logger.error("Bark 推送失败: %s", str(e))
        return False


def _build_bark_url(title, body, group="QDII"):
    """构造 Bark GET 模式 URL（备用），使用 urllib.parse.quote 编码"""
    t = urllib.parse.quote(title, safe="")
    b = urllib.parse.quote(body, safe="")
    return f"{BARK_SERVER}/{BARK_KEY}/{t}/{b}?group={group}"


# ── 企业微信 Webhook ─────────────────────────────────────


def send_wechat_webhook(content):
    """通过企业微信 Webhook 发送 Markdown 消息"""
    if not WECHAT_WEBHOOK_URL:
        logger.debug("企业微信 Webhook 未配置，跳过")
        return False

    try:
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }
        resp = requests.post(
            WECHAT_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") == 0:
            logger.info("企业微信推送成功")
            return True
        else:
            logger.warning("企业微信推送返回异常: %s", data)
            return False
    except Exception as e:
        logger.error("企业微信推送失败: %s", str(e))
        return False


# ── SMTP 邮件 ────────────────────────────────────────────


def _is_smtp_configured():
    """检查 SMTP 是否已配置"""
    return all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_RECEIVER])


def send_change_email(changes):
    """发送限额变动 HTML 邮件"""
    if not changes:
        return False

    if not _is_smtp_configured():
        logger.debug("SMTP 未配置，跳过邮件发送")
        return False

    try:
        rows_html = ""
        for c in changes:
            old_display = _format_limit(c.get("old_limit"))
            new_display = _format_limit(c.get("new_limit"))

            if c.get("new_limit", 0) == 0:
                color = "#e74c3c"
            elif (c.get("new_limit", 0) or 0) > (c.get("old_limit", 0) or 0):
                color = "#2ecc71"
            else:
                color = "#f39c12"

            rows_html += f"""
            <tr>
                <td style="padding:8px;border:1px solid #ddd">{c['code']}</td>
                <td style="padding:8px;border:1px solid #ddd">{c['name']}</td>
                <td style="padding:8px;border:1px solid #ddd">{old_display}</td>
                <td style="padding:8px;border:1px solid #ddd;color:{color};font-weight:bold">
                    {new_display}
                </td>
            </tr>"""

        html = f"""
        <html>
        <body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px">
            <div style="max-width:600px;margin:auto;background:#fff;border-radius:8px;
                        box-shadow:0 2px 8px rgba(0,0,0,0.1);padding:20px">
                <h2 style="color:#2c3e50;margin-top:0">📊 QDII 基金限额变动通知</h2>
                <p style="color:#666">共 {len(changes)} 只基金发生限额变化：</p>
                <table style="width:100%;border-collapse:collapse;margin:15px 0">
                    <tr style="background:#34495e;color:#fff">
                        <th style="padding:10px;text-align:left">代码</th>
                        <th style="padding:10px;text-align:left">名称</th>
                        <th style="padding:10px;text-align:left">旧限额</th>
                        <th style="padding:10px;text-align:left">新限额</th>
                    </tr>
                    {rows_html}
                </table>
                <p style="color:#999;font-size:12px">—— QDII 哨兵 Pro 自动发送</p>
            </div>
        </body>
        </html>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"⚠️ QDII 限额变动 — {len(changes)} 只基金"
        msg["From"] = SMTP_USER
        msg["To"] = SMTP_RECEIVER
        msg.attach(MIMEText(html, "html", "utf-8"))

        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
            server.starttls()

        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [SMTP_RECEIVER], msg.as_string())
        server.quit()

        logger.info("邮件发送成功: %d 条变动通知", len(changes))
        return True

    except Exception as e:
        logger.error("邮件发送失败: %s", str(e))
        return False


# ── 统一通知入口（幂等推送） ─────────────────────────────


def notify_all(changes):
    """
    通过所有已配置的通道发送限额变动通知。
    启用 push_log 去重后，只有状态真正变化的基金才会推送。
    任一通道失败不影响其他通道。
    """
    if not changes:
        return

    # 去重过滤
    if PUSH_DEDUP_ENABLED:
        from models import should_push, record_push
        filtered = []
        for c in changes:
            code = c["code"]
            limit = c.get("new_limit", -1.0)
            status = _limit_status(limit)
            if should_push(code, limit, status):
                filtered.append(c)
            else:
                logger.debug("推送去重: %s %s 状态未变，跳过", code, c.get("name", ""))
        if not filtered:
            logger.info("所有变动均已推送过，跳过本次通知")
            return
        changes = filtered

    count = len(changes)
    logger.info("准备发送通知: %d 条变动", count)

    # 1. Bark 推送
    bark_title = f"QDII 限额变动 ({count}只)"
    bark_lines = []
    for c in changes[:5]:  # Bark 正文不宜过长，只取前 5 条
        old = _format_limit(c.get("old_limit"))
        new = _format_limit(c.get("new_limit"))
        bark_lines.append(f"{c['name']}: {old} → {new}")
    if count > 5:
        bark_lines.append(f"...还有 {count - 5} 条")
    bark_body = "\n".join(bark_lines)
    bark_ok = send_bark_push(bark_title, bark_body)

    # 2. 企业微信
    wechat_lines = [f"## ⚠️ QDII 限额变动 ({count}只)\n"]
    for c in changes[:10]:
        old = _format_limit(c.get("old_limit"))
        new = _format_limit(c.get("new_limit"))
        wechat_lines.append(f"> **{c['code']}** {c['name']}：{old} → <font color=\"warning\">{new}</font>")
    if count > 10:
        wechat_lines.append(f"\n...还有 {count - 10} 条变动")
    send_wechat_webhook("\n".join(wechat_lines))

    # 3. 邮件
    send_change_email(changes)

    # 4. 记录已推送（去重用）
    if PUSH_DEDUP_ENABLED:
        from models import record_push
        for c in changes:
            code = c["code"]
            limit = c.get("new_limit", -1.0)
            status = _limit_status(limit)
            channel = "bark" if bark_ok else "wechat"
            record_push(code, limit, status, channel)


# ── 每日 TOP5 推荐推送 ───────────────────────────────────


def _fund_type_badge(fund_type):
    """根据基金类型返回 HTML 颜色徽章"""
    colors = {
        "股票-美股": ("#e8f5e9", "#2e7d32"),
        "股票-港股": ("#fff3e0", "#e65100"),
        "股票-全球": ("#e3f2fd", "#1565c0"),
        "债券": ("#fce4ec", "#c62828"),
        "商品": ("#fff8e1", "#f9a825"),
        "房地产": ("#f3e5f5", "#7b1fa2"),
        "混合/其他": ("#f5f5f5", "#616161"),
    }
    bg, fg = colors.get(fund_type, colors["混合/其他"])
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        f'font-size:11px;font-weight:600;background:{bg};color:{fg}">'
        f'{fund_type}</span>'
    )


def _score_color(score):
    """评分着色"""
    if score >= 70:
        return "#2e7d32"
    if score >= 40:
        return "#f9a825"
    return "#c62828"


def send_daily_top5(top_list):
    """
    发送每日 TOP N 推荐推送。
    1. Bark: 简洁文字摘要 (含基金代码)
    2. 邮件: HTML 精美卡片 + 天天基金链接
    3. 企业微信: Markdown
    """
    if not top_list:
        logger.info("TOP 无数据，跳过推送")
        return

    from datetime import datetime
    from config import TOP_N
    today = datetime.now().strftime("%m月%d日")
    n = len(top_list)

    # ── 1. Bark 推送（含基金代码）──
    bark_title = f"📊 QDII 每日 TOP{n} ({today})"
    bark_lines = []
    for i, f in enumerate(top_list, 1):
        t = f["fund_type"]
        s = f["score"]
        bark_lines.append(f"{i}. [{t}] {f['code']} {f['name']} — {s:.0f}分")
    bark_body = "\n".join(bark_lines)
    # 点击通知跳转第一名基金的天天基金页面
    top1_url = f"http://fund.eastmoney.com/{top_list[0]['code']}.html"
    send_bark_push(bark_title, bark_body, group="QDII推荐", url=top1_url)

    # ── 2. HTML 邮件（精美 + 可点击链接）──
    if _is_smtp_configured():
        rows_html = ""
        for i, f in enumerate(top_list, 1):
            fund_url = f"http://fund.eastmoney.com/{f['code']}.html"
            type_badge = _fund_type_badge(f["fund_type"])
            sc = f["score"]
            sc_color = _score_color(sc)
            sc_width = max(5, min(100, sc))

            # 限额展示
            la = f.get("limit_amount", -1)
            if la >= 999999:
                limit_str = '<span style="color:#2e7d32">不限</span>'
            elif la == 0:
                limit_str = '<span style="color:#c62828">暂停</span>'
            elif la == -2:
                limit_str = '<span style="color:#f9a825">限大额</span>'
            elif la > 0:
                limit_str = f'{la:.2f}万'
            else:
                limit_str = '未知'

            # 日涨跌
            dg = f.get("day_growth", 0) * 100
            if dg > 0:
                growth_str = f'<span style="color:#c62828">+{dg:.2f}%</span>'
            elif dg < 0:
                growth_str = f'<span style="color:#2e7d32">{dg:.2f}%</span>'
            else:
                growth_str = '0.00%'

            # 最大回撤
            dd = f.get("max_drawdown", 0) * 100
            dd_str = f'{dd:.1f}%' if dd > 0 else '-'

            rows_html += f"""
            <tr style="border-bottom:1px solid #f0f0f0">
                <td style="padding:14px 10px;text-align:center;font-size:18px;font-weight:700;color:{sc_color}">
                    {i}
                </td>
                <td style="padding:14px 10px">
                    <div>
                        <a href="{fund_url}" style="color:#1a73e8;text-decoration:none;font-weight:600;font-size:14px"
                           target="_blank">{f['name']}</a>
                    </div>
                    <div style="margin-top:4px">
                        {type_badge}
                        <span style="color:#999;font-size:11px;margin-left:6px">{f['code']}</span>
                    </div>
                </td>
                <td style="padding:14px 10px;text-align:center">
                    <div style="font-size:18px;font-weight:700;color:{sc_color}">{sc:.0f}</div>
                    <div style="width:50px;height:4px;background:#eee;border-radius:2px;margin:4px auto 0">
                        <div style="width:{sc_width}%;height:100%;background:{sc_color};border-radius:2px"></div>
                    </div>
                </td>
                <td style="padding:14px 10px;text-align:center;font-size:13px">{limit_str}</td>
                <td style="padding:14px 10px;text-align:center;font-size:13px;font-weight:600">
                    {f.get('current_nav', 0):.4f}
                </td>
                <td style="padding:14px 10px;text-align:center;font-size:13px">{growth_str}</td>
                <td style="padding:14px 10px;text-align:center;font-size:12px;color:#999">{dd_str}</td>
            </tr>"""

        html = f"""
        <html>
        <body style="font-family:'Segoe UI',Arial,sans-serif;background:#f7f8fc;padding:20px;margin:0">
            <div style="max-width:700px;margin:auto;background:#fff;border-radius:12px;
                        box-shadow:0 2px 16px rgba(0,0,0,0.08);overflow:hidden">
                <div style="background:linear-gradient(135deg,#667eea,#764ba2);padding:24px 28px;color:#fff">
                     <h1 style="margin:0;font-size:20px;font-weight:700">📊 QDII 每日 TOP{n} 推荐</h1>
                    <p style="margin:8px 0 0;opacity:0.85;font-size:13px">{today} · QDII 哨兵 Pro 自动生成</p>
                </div>
                <table style="width:100%;border-collapse:collapse">
                    <thead>
                        <tr style="background:#f9fafb">
                            <th style="padding:12px 10px;font-size:11px;color:#999;font-weight:600;text-align:center;width:40px">#</th>
                            <th style="padding:12px 10px;font-size:11px;color:#999;font-weight:600;text-align:left">基金</th>
                            <th style="padding:12px 10px;font-size:11px;color:#999;font-weight:600;text-align:center;width:70px">评分</th>
                            <th style="padding:12px 10px;font-size:11px;color:#999;font-weight:600;text-align:center;width:60px">限额</th>
                            <th style="padding:12px 10px;font-size:11px;color:#999;font-weight:600;text-align:center;width:70px">净值</th>
                            <th style="padding:12px 10px;font-size:11px;color:#999;font-weight:600;text-align:center;width:60px">日涨跌</th>
                            <th style="padding:12px 10px;font-size:11px;color:#999;font-weight:600;text-align:center;width:60px">最大回撤</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
                <div style="padding:16px 28px;background:#f9fafb;border-top:1px solid #eee">
                    <p style="margin:0;font-size:11px;color:#aaa;text-align:center">
                        点击基金名称可跳转天天基金查看详情 · QDII 哨兵 Pro
                    </p>
                </div>
            </div>
        </body>
        </html>
        """

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"📊 QDII 每日 TOP{n} 推荐 ({today})"
            msg["From"] = SMTP_USER
            msg["To"] = SMTP_RECEIVER
            msg.attach(MIMEText(html, "html", "utf-8"))

            if SMTP_PORT == 465:
                server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
            else:
                server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
                server.starttls()

            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [SMTP_RECEIVER], msg.as_string())
            server.quit()
            logger.info("TOP%d 邮件发送成功", n)
        except Exception as e:
            logger.error("TOP%d 邮件发送失败: %s", n, str(e))

    # ── 3. 企业微信 Markdown ──
    wechat_lines = [f"## 📊 QDII 每日 TOP{n} ({today})\n"]
    for i, f in enumerate(top_list, 1):
        fund_url = f"http://fund.eastmoney.com/{f['code']}.html"
        t = f["fund_type"]
        s = f["score"]
        wechat_lines.append(
            f"> **{i}.** `{f['code']}` `[{t}]` [{f['name']}]({fund_url}) — "
            f"<font color=\"info\">{s:.0f}分</font>"
        )
    send_wechat_webhook("\n".join(wechat_lines))

    logger.info("每日 TOP%d 推送完成", n)


# ── 深度扫描完成推送 ─────────────────────────────────────


def send_deep_scan_summary(success, fail, top_list=None):
    """
    深度扫描完成后发送汇总推送。
    包含扫描统计和当前 TOP N 评分。
    """
    from datetime import datetime
    now = datetime.now().strftime("%H:%M")
    n = len(top_list) if top_list else 0

    # ── 1. Bark 推送 ──
    bark_title = f"🔍 深度扫描完成 ({now})"
    bark_lines = [f"成功: {success}  失败: {fail}"]
    if top_list:
        bark_lines.append("")
        bark_lines.append(f"当前 TOP{n}:")
        for i, f in enumerate(top_list, 1):
            bark_lines.append(
                f"{i}. {f['code']} {f['name']} — {f.get('score', 0):.0f}分"
            )
    bark_body = "\n".join(bark_lines)
    send_bark_push(bark_title, bark_body, group="QDII扫描")

    # ── 2. 企业微信 ──
    wechat_lines = [f"## 🔍 深度扫描完成 ({now})\n"]
    wechat_lines.append(f"> 成功: **{success}** | 失败: **{fail}**\n")
    if top_list:
        wechat_lines.append(f"**当前 TOP{n}:**")
        for i, f in enumerate(top_list, 1):
            sc = f.get("score", 0)
            wechat_lines.append(
                f"> **{i}.** `{f['code']}` {f['name']} — <font color=\"info\">{sc:.0f}分</font>"
            )
    send_wechat_webhook("\n".join(wechat_lines))

    logger.info("深度扫描汇总推送完成")


# ── 基础扫描完成推送 ─────────────────────────────────────


def send_basic_scan_summary(success, fail, change_count):
    """
    基础扫描完成后发送汇总推送。
    """
    from datetime import datetime
    now = datetime.now().strftime("%H:%M")

    # ── Bark 推送 ──
    bark_title = f"📋 基础扫描完成 ({now})"
    bark_lines = [f"成功: {success}  失败: {fail}  变动: {change_count}"]
    if change_count == 0:
        bark_lines.append("本次无限额变动")
    bark_body = "\n".join(bark_lines)
    send_bark_push(bark_title, bark_body, group="QDII扫描")

    # ── 企业微信 ──
    wechat_lines = [f"## 📋 基础扫描完成 ({now})\n"]
    wechat_lines.append(f"> 成功: **{success}** | 失败: **{fail}** | 变动: **{change_count}**")
    send_wechat_webhook("\n".join(wechat_lines))

    logger.info("基础扫描汇总推送完成")
