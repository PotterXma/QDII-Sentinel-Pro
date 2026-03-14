# QDII Sentinel Pro

> 运维级 Windows 桌面 QDII 基金深度监控系统

## 功能概览

| 功能 | 说明 |
|--|--|
| 🛡️ 系统托盘 | 静默驻留右下角, 右键菜单操作 |
| 📊 实时看板 | 浏览器打开本地 Flask 仪表盘 |
| 🔔 Bark 推送 | 限额变动 → iPhone 即时通知 |
| 💱 汇率追踪 | USD/CNY 实时汇率 + 风险评估 |
| 📈 深度分析 | 最大回撤 / Mag-7 持仓 / 费率体检 |
| 🏆 智能推荐 | 5 维加权评分 → TOP-5 基金推荐 |
| 🔒 单实例 | 自动检测, 防止重复启动 |

## 快速开始

### 开发环境运行

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```

### 打包为 EXE

```bash
# 一键打包
build.bat
```

打包产物在 `dist/` 目录:
- `QDII_Sentinel.exe` — 主程序
- `config.ini` — 配置文件（放在 exe 同目录）

### 部署

1. 将 `QDII_Sentinel.exe` 和 `config.ini` 复制到目标目录
2. 编辑 `config.ini`, 配置 Bark Key 等
3. 双击运行, 图标出现在右下角

## 配置说明

编辑 `config.ini`（和 exe 放在同一目录）:

```ini
[Bark]
push_key = 你的BarkKey

[General]
schedule_hours = 2        # 扫描间隔（小时）
flask_port = 5000         # 看板端口

[SMTP]
user = your@qq.com        # 留空则不启用邮件
password = your_auth_code
```

## 文件结构

```
QDII_Sentinel/
├── config.ini         # 用户配置（外部, 可编辑）
├── main.py            # 入口: 托盘 + Flask + 调度
├── config.py          # INI 加载器
├── models.py          # SQLite 数据模型
├── scraper.py         # 基金爬虫
├── deep_scraper.py    # 深度数据采集
├── exchange_rate.py   # 汇率追踪
├── analyzer.py        # 评分引擎
├── notifier.py        # 通知模块 (Bark/邮件/微信)
├── app.py             # Flask Web 应用
├── templates/         # HTML 模板
├── build.bat          # PyInstaller 打包脚本
├── logs/              # 日志输出目录 (自动创建)
└── qdii_sentinel.db   # SQLite 数据库 (自动创建)
```

## 系统要求

- Windows 10/11
- Python 3.9+ (开发环境)
