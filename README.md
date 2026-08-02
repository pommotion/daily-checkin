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

#### 站点配置

| Secret 名 | 说明 |
|---|---|
| `HITUN_CURL_BASH` | 海豚湾签到 curl bash |
| `RENZHE_EMAIL` | 忍者云登录邮箱 |
| `RENZHE_PASSWD` | 忍者云登录密码 |
| `LISTENHUB_FREE_CURL_BASH` | ListenHub 免费账号签到 curl bash |
| `LISTENHUB_PRO_CURL_BASH` | ListenHub 会员账号签到 curl bash |

<!-- 已移除：IDKEY_CURL_BASH — Cloudflare cf_clearance IP 绑定，GitHub Actions 无法绕过 -->

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
- **忍者云**: 已移除（Passkey 强制验证）
- **ListenHub**: JWT ~60 天过期，需重新抓包
- **idkey**: 已移除（CF IP 绑定）

签到失败时报告会标注哪个站需更新，推送通知会提醒。

---

## 🛡️ 三层保活机制

自动签到有三种死法，每种对应一层防护。

### L1 仓库保活 — 防 GitHub 60 天自动禁用

GitHub 对公开仓库有个规则：**60 天无仓库活动就自动停掉 schedule 触发**。双重防护：

| 手段 | 原理 |
|---|---|
| `Commit state` step | 每天把 `state.json` 提交回仓库，仓库持续有新 commit |
| `keepalive` job | 调 GitHub API 直接重置计时器，**不依赖有无 commit** |

两道保险相互兵分两路：即使 state 长期不变、或 commit 步骤失败，keepalive 依然生效。

### L2 凭证保活 — 在过期前预警

凭证都有寿命，过期后只会得到一串看不出原因的失败。`health.py` 在**每次签到前**先体检：

| 凭证类型 | 检测方式 |
|---|---|
| JWT（ListenHub） | 直接解开 payload 读 `exp`，算出精确剩余天数 |
| cf_clearance（海豚湾） | 内容不可解析，改用「首见日期 + 经验寿命 30 天」推算 |

剩余 **≤ 7 天**开始在报告里提醒，已过期则升级为 🚨。

> 更新凭证后不需要手动重置——指纹变化会被识别为「已更新」并自动重新计时。
> `state.json` 只存凭证的 SHA256 前 16 位，**不存明文**。

### L3 结果保活 — 防静默失败

Action 显示 success ≠ 真的签到了。单次运行看不出「这个站已经连续 5 天失败」，
`state.json` 提交回仓库后，每次运行都能读到历史：

- 连续失败 **≥ 3 天** → 报告标题升级为 `🚨 签到异常 — 需人工介入`
- 失败站点下方附一行「连续失败 N 天，最后成功 X」
- 恢复成功后自动归零并记录 `recovered_at`

### 报告标题分级

| 标题 | 触发条件 |
|---|---|
| 📅 每日签到报告 | 一切正常 |
| ⚠️ 凭证即将过期 | 有凭证 ≤ 7 天到期 |
| 🚨 签到异常 — 需人工介入 | 连续失败 ≥ 3 天，或凭证已过期 |

目的是让真正需要处理的问题从日常日报里跳出来，而不是淹没在一片 ✅ 里。

### state.json 结构

```json
{
  "sites": {
    "海豚湾": {
      "fail_streak": 0,
      "last_success": "2026-08-02",
      "total_runs": 42,
      "total_fails": 3
    }
  },
  "credentials": {
    "ListenHub-会员": { "jwt_exp": "2026-09-15T...", "jwt_days_left": 44 },
    "海豚湾": { "cf_fingerprint": "a1b2c3...", "cf_first_seen": "2026-07-20" }
  },
  "runs": [{ "date": "2026-08-02", "success": 3, "fail": 0 }]
}
```

只保留最近 30 次运行摘要，不会无限膨胀。
