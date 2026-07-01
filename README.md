# 📅 daily-checkin

多站点每日自动签到，一个仓库管所有站点，一个 cron 跑全部。

## ✨ 特性

- **多站点统一管理**：一个仓库、一个 cron、一份汇总报告
- **curl_bash 回放模式**：浏览器复制粘贴即可接入新站点，不需要逆向 API
- **共享推送**：配一次 Telegram/PushPlus/WxPusher/Server酱，全局生效
- **5 分钟加新站**：在 `sites.py` 加几行配置 + 在 GitHub 加一个 Secret

## 🏗️ 架构

```
daily-checkin/
├── main.py              # 入口：遍历站点 → 签到 → 汇总 → 推送
├── curl_parser.py       # curl_bash 解析器（提取 url/header/cookie/body）
├── push.py              # 共享推送（Telegram/PushPlus/WxPusher/Server酱）
├── sites.py             # 站点配置（加站点只改这里）
├── log_utils.py         # 日志
├── requirements.txt     # 依赖：requests
└── .github/workflows/
    └── checkin.yml      # GitHub Action（每天北京 08:00）
```

### 核心设计

所有签到本质上是同一个流程：**认证 → 请求签到端点 → 判定结果 → 通知**

站点之间唯一不同的只有：认证方式、端点 URL、成功/失败关键词。
curl_bash 回放模式已能覆盖绝大多数签到场景。

## 🚀 部署

### 1. Fork 或 Clone 本仓库

### 2. 配置 GitHub Secrets

进入仓库 `Settings → Secrets and variables → Actions → New repository secret`

#### 站点配置（curl_bash 整段）

| Secret 名 | 说明 |
|---|---|
| `HITUN_CURL_BASH` | 海豚湾签到 curl bash |
| `RENZHE_CURL_BASH` | 忍者云签到 curl bash |
| `LISTENHUB_FREE_CURL_BASH` | ListenHub 免费账号签到 curl bash |
| `LISTENHUB_PRO_CURL_BASH` | ListenHub 会员账号签到 curl bash |

#### 推送配置

| Secret 名 | 说明 |
|---|---|
| `PUSH_METHOD` | 推送渠道：`telegram` / `pushplus` / `wxpusher` / `serverchan` |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID |

### 3. 启用 Actions

进入仓库 `Actions` 页面 → `I understand my workflows, go ahead and enable them`

### 4. 手动触发测试

`Actions → daily-checkin → Run workflow`

## 🔑 如何获取 curl_bash

1. 浏览器登录目标网站
2. `F12` → `Network` 标签 → 勾选 `Fetch/XHR`
3. 点击签到按钮
4. 找到签到请求 → 右键 → `Copy → Copy as cURL (bash)`
5. 粘贴到 GitHub Secret 中

## ➕ 加新站点

1. 在 `sites.py` 的 `SITES` 列表中添加：
```python
{
    "name": "新站点名",
    "curl_bash_env": "NEW_SITE_CURL_BASH",
    "success_keywords": ["成功", "获得"],
    "already_keywords": ["已签到"],
    "auth_fail_keywords": ["未登录"],
    "cf_fail_keywords": ["cloudflare"],
    "enabled": True,
},
```

2. 在 `.github/workflows/checkin.yml` 的 `env` 中添加：
```yaml
NEW_SITE_CURL_BASH: ${{ secrets.NEW_SITE_CURL_BASH }}
```

3. 在 GitHub 仓库 `Settings → Secrets` 中添加 `NEW_SITE_CURL_BASH`

完成。不需要写任何 Python 代码。

## 📋 每日报告示例

```
📅 每日签到报告
🕐 2026-07-01 08:00:07

✅ 海豚湾签到成功 — 续命成功
✅ 忍者云签到成功 — 获得了 233 MB流量
✅ ListenHub-免费签到成功 — +5 credits
✅ ListenHub-会员签到成功 — +15 credits

📊 汇总: ✅ 成功 4 | ❌ 失败 0
```

## ⏰ Cookie/Token 更新

- **hitun.io**: cf_clearance ~30 天过期，需重新抓包
- **忍者云**: Cookie 可能更短命，支持账号密码自动登录（未来优化）
- **ListenHub**: JWT ~60 天过期，需重新抓包

签到失败时报告会标注哪个站需更新，推送通知会提醒。
