<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-blue?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows" />
  <img src="https://img.shields.io/badge/License-MIT-green" />
  <img src="https://img.shields.io/github/stars/PotterXma/QDII-Sentinel-Pro?style=social" />
</p>

<h1 align="center">🛡️ QDII Sentinel Pro</h1>

<p align="center">
  <b>运维级 Windows 桌面 QDII 基金深度监控系统</b><br/>
  <i>实时追踪限额变动 · 多维度智能评分 · 多渠道告警推送 · 本地可视化看板</i>
</p>

---

## ✨ 核心功能

| 模块 | 功能描述 | 关键技术 |
|:---:|---|---|
| 🛡️ **系统托盘** | 静默驻留系统右下角，右键菜单控制全局 | pystray + 单实例锁 |
| 📊 **实时看板** | 浏览器打开本地 Flask 仪表盘，基金详情一目了然 | Flask + Jinja2 模板 |
| 🔔 **多渠道推送** | Bark (iOS) / 企业微信 Webhook / SMTP 邮件 | 可配置多通道并行 |
| 💱 **汇率追踪** | USD/CNY 实时汇率 + 历史趋势 + 汇率风险评估 | open.er-api.com |
| 📈 **深度分析** | 最大回撤 / 费率体检 / 持仓穿透 / 历史净值回溯 | 多线程并发采集 |
| 🏆 **智能评分** | 5 维加权评分模型 → TOP-5 每日推荐推送 | 可调权重评分引擎 |
| 🔒 **单实例保护** | 端口占用检测，防止重复启动 | socket bind |
| ⏰ **四路调度** | 基础扫描 / 深度扫描 / 汇率更新 / 每日推荐 | APScheduler |

## 📸 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                     QDII Sentinel Pro                       │
├──────────┬──────────────┬───────────────┬───────────────────┤
│  主线程  │   守护线程 1  │  守护线程 2   │    初始化线程      │
│ pystray  │ APScheduler  │ Flask Server  │  首次扫描+汇率     │
│ 托盘循环 │  四路定时任务  │ 127.0.0.1:5000│  (非阻塞启动)     │
├──────────┴──────────────┴───────────────┴───────────────────┤
│              单实例锁 (socket port 59123)                    │
└─────────────────────────────────────────────────────────────┘

定时任务:
  ├── 基础扫描 ──── 每 N 小时 ──── 限额 + 净值抓取
  ├── 深度扫描 ──── 每 N 小时 ──── 历史净值/持仓/费率 → 评分
  ├── 汇率更新 ──── 每 N 小时 ──── USD/CNY 实时汇率
  └── TOP5 推荐 ─── 每日定时  ──── 智能推荐 → 多渠道推送
```

## 🏆 智能评分模型

五维加权评分体系，全方位评估 QDII 基金质量：

| 维度 | 默认权重 | 评估内容 |
|:---:|:---:|---|
| 📏 限额可用性 | 30% | 申购限额是否充足（低于阈值评分为 0） |
| 📉 最大回撤 | 20% | 历史最大回撤幅度，衡量风险控制能力 |
| 💱 汇率收益 | 20% | 结合汇率变动的综合收益评估 |
| 🏢 资产质量 | 20% | 持仓集中度、资产配置合理性 |
| 💰 费用成本 | 10% | 管理费 + 托管费 + 申购费综合成本 |

## 🚀 快速开始

### 环境要求

- **操作系统**: Windows 10/11
- **Python**: 3.9+
- **网络**: 需要访问天天基金 API 和汇率接口

### 开发环境运行

```bash
# 1. 克隆项目
git clone https://github.com/PotterXma/QDII-Sentinel-Pro.git
cd QDII-Sentinel-Pro

# 2. 创建虚拟环境（推荐）
python -m venv .venv
.venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 编辑配置文件
copy .env.example config.ini
# 按需修改 config.ini

# 5. 启动
python main.py
```

### 打包为 EXE

```bash
# 一键打包（PyInstaller）
build.bat
```

产物在 `dist/` 目录：
- `QDII_Sentinel.exe` — 主程序（单文件，无需 Python 环境）

### 部署

1. 将 `QDII_Sentinel.exe` 和 `config.ini` 复制到目标目录
2. 编辑 `config.ini` 配置推送通道和扫描参数
3. 双击运行 → 图标出现在系统托盘右下角
4. 右键托盘图标 → 📊 打开看板 / 🔄 立即执行 / ❌ 退出

## ⚙️ 配置说明

所有配置集中在 `config.ini` 文件中，放在 exe 同目录或 `%APPDATA%\QDII_Sentinel\` 下：

```ini
[General]
schedule_hours = 12          # 基础扫描间隔（小时）
deep_scan_hours = 24         # 深度扫描间隔（小时）
fx_update_hours = 12         # 汇率更新间隔（小时）
flask_port = 5000            # 看板端口
monitor_all_qdii = True      # 监控全部 QDII（False 则仅美股相关）
daily_push_hour = 8          # 每日 TOP5 推送时间
daily_push_minute = 30

[Bark]
push_key = YOUR_BARK_KEY     # Bark 推送 Key（iOS 通知）
server = https://api.day.app # Bark 服务器地址

[SMTP]
host = smtp.qq.com           # 邮件服务器
port = 465
user =                       # 邮箱账号（留空不启用）
password =                   # 授权码
receiver =                   # 收件人

[WeChat]
webhook_url =                # 企业微信机器人 Webhook（留空不启用）

[ExchangeRate]
api_url = https://open.er-api.com/v6/latest/USD

[DeepScan]
batch_size = 50              # 每批扫描基金数
batch_delay = 60             # 批次间隔（秒）
request_delay_min = 2        # 请求随机延迟（秒）
request_delay_max = 5
max_failures = 5             # 连续失败阈值
workers = 8                  # 并发线程数

[Scoring]
weight_limit = 0.30          # 限额权重
weight_drawdown = 0.20       # 回撤权重
weight_fx_return = 0.20      # 汇率收益权重
weight_asset_quality = 0.20  # 资产质量权重
weight_cost = 0.10           # 费用成本权重
limit_threshold_yuan = 10    # 限额阈值（元）

[PushDedup]
enabled = true               # 推送去重（防止重复报警）
```

## 📁 项目结构

```
QDII-Sentinel-Pro/
├── main.py              # 🚀 主入口: 托盘 + Flask + 调度器
├── config.py            # ⚙️ INI 配置加载器 (PyInstaller 适配)
├── config.ini           # 📋 用户配置文件
├── models.py            # 🗄️ SQLite 数据模型 (ORM)
├── scraper.py           # 🕷️ 基金数据爬虫 (限额/净值)
├── deep_scraper.py      # 🔬 深度数据采集 (历史净值/持仓/费率)
├── deep_scanner.py      # 📡 深度扫描调度器 (批量并发)
├── analyzer.py          # 📊 数据分析引擎 (回撤/趋势)
├── scorer.py            # 🏆 五维评分引擎
├── exchange_rate.py     # 💱 汇率数据采集
├── fx_tracker.py        # 📈 汇率追踪管理
├── notifier.py          # 🔔 多渠道推送 (Bark/SMTP/WeChat)
├── app.py               # 🌐 Flask Web 应用
├── templates/           # 🎨 HTML 模板
│   ├── index.html       #    主看板页面
│   ├── fund_detail.html #    基金详情页
│   ├── history.html     #    历史记录页
│   └── exchange_rate.html#   汇率追踪页
├── requirements.txt     # 📦 Python 依赖
├── build.bat            # 🔨 PyInstaller 打包脚本
├── .env.example         # 📝 配置模板
└── .gitignore           # 🙈 Git 忽略规则
```

## 📦 技术栈

| 类别 | 技术 |
|:---:|---|
| **语言** | Python 3.9+ |
| **Web 框架** | Flask 2.3+ |
| **任务调度** | APScheduler 3.10+ |
| **系统托盘** | pystray 0.19+ |
| **图像处理** | Pillow 10.0+ |
| **HTTP 客户端** | Requests 2.31+ / urllib3 2.0+ |
| **数据库** | SQLite 3 (内置) |
| **打包工具** | PyInstaller |
| **数据源** | 天天基金 (eastmoney.com) |

## 📊 数据存储

运行时数据自动存储在 `%APPDATA%\QDII_Sentinel\`：

```
%APPDATA%\QDII_Sentinel\
├── data\
│   └── qdii_sentinel.db    # SQLite 数据库
└── logs\
    └── qdii_sentinel.log   # 按天轮转，保留 30 天
```

## 🤝 参与贡献

1. Fork 本仓库
2. 创建功能分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'feat: add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 提交 Pull Request

## 📄 License

本项目采用 [MIT License](LICENSE) 开源协议。

---

<p align="center">
  <b>⭐ 如果觉得有用，请点个 Star 支持一下！</b>
</p>
