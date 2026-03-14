"""
QDII 哨兵 Pro 配置模块
从 config.ini 读取所有配置
+ PyInstaller 路径适配 (sys._MEIPASS)
+ %APPDATA%/QDII_Sentinel/ 日志与数据库
"""

import os
import sys
import configparser

# ── 路径动态化 (PyInstaller 适配) ────────────────────────

def resource_path(relative_path):
    """
    获取资源文件的绝对路径。
    PyInstaller 打包后资源在 sys._MEIPASS 临时目录中，
    开发环境下则取源码目录。
    """
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


# exe 所在目录（打包后）或源码目录（开发时）
if getattr(sys, 'frozen', False):
    EXE_DIR = os.path.dirname(sys.executable)
else:
    EXE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 目录配置 ─────────────────────────────────────────────

# 根据审计要求：默认数据库路径及日志应设为 %APPDATA%/QDII_Sentinel/
APP_DATA_ROOT = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'QDII_Sentinel')

DATA_DIR = os.path.join(EXE_DIR, "data")
LOG_DIR = os.path.join(EXE_DIR, "logs")

for _dir in (LOG_DIR, DATA_DIR):
    os.makedirs(_dir, exist_ok=True)

# ── 读取 config.ini ──────────────────────────────────────
# 搜索顺序: exe 同目录 → APPDATA → 源码目录

_cfg = configparser.ConfigParser()
_cfg_candidates = [
    os.path.join(EXE_DIR, "config.ini"),
    resource_path("config.ini"),
]
_cfg_found = False
for _path in _cfg_candidates:
    if os.path.exists(_path):
        _cfg.read(_path, encoding="utf-8")
        _cfg_found = True
        CONFIG_INI_PATH = _path
        break

if not _cfg_found:
    raise FileNotFoundError(
        f"配置文件 config.ini 未找到，已搜索: {_cfg_candidates}"
    )

# ── [General] ────────────────────────────────────────────

SCHEDULE_HOURS = _cfg.getint("General", "schedule_hours", fallback=12)
DEEP_SCAN_HOURS = _cfg.getint("General", "deep_scan_hours", fallback=24)
FX_UPDATE_HOURS = _cfg.getint("General", "fx_update_hours", fallback=12)
FLASK_PORT = _cfg.getint("General", "flask_port", fallback=5000)
MONITOR_ALL_QDII = _cfg.getboolean("General", "monitor_all_qdii", fallback=True)
DAILY_PUSH_HOUR = _cfg.getint("General", "daily_push_hour", fallback=8)
DAILY_PUSH_MINUTE = _cfg.getint("General", "daily_push_minute", fallback=30)

# ── 数据库 ───────────────────────────────────────────────

DB_PATH = os.path.join(EXE_DIR, "qdii_sentinel.db")

# ── [Bark] ───────────────────────────────────────────────

BARK_KEY = _cfg.get("Bark", "push_key", fallback="")
BARK_SERVER = _cfg.get("Bark", "server", fallback="https://api.day.app").rstrip("/")
BARK_SOUND = _cfg.get("Bark", "sound", fallback="minuet")
BARK_LEVEL = _cfg.get("Bark", "level", fallback="timeSensitive")  # active, timeSensitive, passive, critical

# ── [SMTP] ───────────────────────────────────────────────

SMTP_HOST = _cfg.get("SMTP", "host", fallback="smtp.qq.com")
SMTP_PORT = _cfg.getint("SMTP", "port", fallback=465)
SMTP_USER = _cfg.get("SMTP", "user", fallback="")
SMTP_PASSWORD = _cfg.get("SMTP", "password", fallback="")
SMTP_RECEIVER = _cfg.get("SMTP", "receiver", fallback="")

# ── [WeChat] ─────────────────────────────────────────────

WECHAT_WEBHOOK_URL = _cfg.get("WeChat", "webhook_url", fallback="")

# ── [ExchangeRate] ───────────────────────────────────────

FX_API_URL = _cfg.get("ExchangeRate", "api_url", fallback="https://open.er-api.com/v6/latest/USD")

# ── [DeepScan] ───────────────────────────────────────────

DS_BATCH_SIZE = _cfg.getint("DeepScan", "batch_size", fallback=50)
DS_BATCH_DELAY = _cfg.getint("DeepScan", "batch_delay", fallback=60)
DS_REQUEST_DELAY_MIN = _cfg.getint("DeepScan", "request_delay_min", fallback=2)
DS_REQUEST_DELAY_MAX = _cfg.getint("DeepScan", "request_delay_max", fallback=5)
DS_MAX_FAILURES = _cfg.getint("DeepScan", "max_failures", fallback=5)
DS_WORKERS = _cfg.getint("DeepScan", "workers", fallback=8)

# ── [Scoring] ────────────────────────────────────────────

SCORE_WEIGHT_LIMIT = _cfg.getfloat("Scoring", "weight_limit", fallback=0.30)
SCORE_WEIGHT_DRAWDOWN = _cfg.getfloat("Scoring", "weight_drawdown", fallback=0.20)
SCORE_WEIGHT_FX_RETURN = _cfg.getfloat("Scoring", "weight_fx_return", fallback=0.20)
SCORE_WEIGHT_ASSET_QUALITY = _cfg.getfloat("Scoring", "weight_asset_quality", fallback=0.20)
SCORE_WEIGHT_COST = _cfg.getfloat("Scoring", "weight_cost", fallback=0.10)
LIMIT_THRESHOLD_YUAN = _cfg.getfloat("Scoring", "limit_threshold_yuan", fallback=500)

# ── [PushDedup] ──────────────────────────────────────────

PUSH_DEDUP_ENABLED = _cfg.getboolean("PushDedup", "enabled", fallback=True)

# ── QDII 美股关键词 ──────────────────────────────────────
# 仅在 MONITOR_ALL_QDII=False 时使用

US_KEYWORDS = [
    "纳斯达克", "纳指", "标普500", "标普", "美国",
    "费城半导体", "科技", "芯片", "道琼斯", "罗素",
]

# ── User-Agent 池 ────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# ── 天天基金 API 端点 ────────────────────────────────────

FUND_LIST_URL = "http://fund.eastmoney.com/js/fundcode_search.js"
FUND_DETAIL_URL = "http://fund.eastmoney.com/{code}.html"
FUND_DATA_URL = "http://fundgz.1234567.com.cn/js/{code}.js"

# ── 单实例锁端口 ────────────────────────────────────────

SINGLETON_PORT = 59123
