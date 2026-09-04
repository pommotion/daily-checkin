#!/usr/bin/env python3
"""LibTV（liblib.tv）每日登录积分 — 订阅权益「每日登录赠送 N 积分」。

原理（2026-09-04 ego-browser 实测逆向）:
  - LibTV 没有签到按钮；积分按「每日登录」发放，是进阶版VIP 的订阅权益
    （i18n: subscriptionBenefitDailyCredits = 每日登录赠送 {num} 积分，
    提示「积分发放额度会随活动周期调整」）。
  - 认证: api2.liblib.art + 自定义头 token（usertoken，44 位 hex）+ webid。
    仅带 cookie 或 Authorization Bearer 都会 401，必须 token 头。
  - 显式登录端点: POST /api/www/userLoginRecord/save（无参数，code 0）。
  - 余额查询: GET /api/www/member/memberPower/list（月度算力 currentPower.balance）
              GET /api/www/member/account?isApp=false（attr.freeUsablePower 免费积分）
    页面头部显示的积分总额 = 月度余额 + 免费积分（实测 4015 + 20 = 4035 一致）。

诚实口径（吸取 baiduwp 答题分虚报的教训）:
  服务端没有「已领取 N 积分」回执 → 报告只报余额，并用 state 里昨日的
  免费积分对比推断今日新增；若连续多日无新增，说明发放机制与推断不符，
  需人工重新核查（不该继续声称「已领取」）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api2.liblib.art"
LOGIN_URL = f"{API_BASE}/api/www/userLoginRecord/save"
POWER_URL = f"{API_BASE}/api/www/member/memberPower/list"
ACCOUNT_URL = f"{API_BASE}/api/www/member/account?isApp=false"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _load_creds(site: dict) -> tuple[str, str]:
    """从 LIBTV_CREDENTIALS 读 {"token": "...", "webid": "..."}，返回 (token, webid)。"""
    raw = os.getenv(site.get("credentials_env", "LIBTV_CREDENTIALS"), "").strip()
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
        return str(data.get("token", "")).strip(), str(data.get("webid", "")).strip()
    except Exception:
        # 兼容纯 token 的写法（无 webid 时接口实测也能通过，但尽量带上）
        return raw, ""


def _code(resp) -> int | None:
    try:
        return resp.json().get("code")
    except Exception:
        return None


def _get_json(s: requests.Session, url: str, retries: int = 2):
    """GET 并解析 JSON，code 500 类瞬时异常自动重试一次。"""
    last = None
    for attempt in range(retries + 1):
        try:
            r = s.get(url, timeout=30)
        except requests.RequestException as e:
            raise e
        if _code(r) == 0:
            return r.json()
        last = r
        if attempt < retries:
            import time

            time.sleep(2)
    return {"code": _code(last), "msg": f"HTTP {last.status_code}", "_raw": last.text[:120]}


def run_libtv_checkin(site: dict, state: dict) -> tuple[bool, str]:
    name = site["name"]
    token, webid = _load_creds(site)
    if not token:
        return False, f"❌ {name}缺少凭证 — 请配置 Secret {site.get('credentials_env', 'LIBTV_CREDENTIALS')}（含 token/webid）"

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "token": token, "Accept": "application/json"})
    if webid:
        s.headers["webid"] = webid

    # ---- 1. 登录记录（每日登录动作） ----
    try:
        r1 = s.post(LOGIN_URL, json={}, timeout=30)
    except requests.RequestException as e:
        return False, f"❌ {name}网络异常: {e}"
    if _code(r1) == 401:
        return False, f"❌ {name}token 已失效 — 请重新从浏览器 Cookie 抓取 usertoken/webid 更新 Secret"
    if _code(r1) not in (0,):
        return False, f"❌ {name}登录记录异常 HTTP {r1.status_code}: {r1.text[:120]}"

    # ---- 2. 余额 ----
    try:
        power = _get_json(s, POWER_URL)
        account = _get_json(s, ACCOUNT_URL)
    except requests.RequestException as e:
        return True, f"✅ {name}登录完成（积分发放已触发），余额查询失败: {e}"

    monthly_balance = monthly_total = None
    plist = (power.get("data") or {}).get("list") or []
    if plist:
        cp = plist[0].get("currentPower") or {}
        monthly_balance = cp.get("balance")
        monthly_total = cp.get("total")
    attr = ((account.get("data") or {}).get("attr") or {})
    free_power = attr.get("freeUsablePower")

    # ---- 3. 与昨日对比推断今日新增 ----
    beijing = timezone(timedelta(hours=8))
    today = datetime.now(beijing).strftime("%Y-%m-%d")
    prev = state.setdefault("libtv", {})
    prev_free = prev.get("free_power")
    parts = []
    if monthly_balance is not None:
        parts.append(f"月度 {monthly_balance}")
        if monthly_total is not None:
            parts[-1] += f"/{monthly_total}"
    if free_power is not None:
        parts.append(f"免费 {free_power}")
    balance_desc = "，余额 " + " + ".join(parts) if parts else ""

    delta_desc = ""
    if free_power is not None and isinstance(free_power, (int, float)):
        prev["free_power"] = free_power
        prev["last_run"] = today
        if prev_free is None:
            delta_desc = "（首次记录，明日开始对比）"
        elif free_power > prev_free:
            delta_desc = f"，今日 +{free_power - prev_free} 已到账"
        elif free_power == prev_free:
            delta_desc = f"，今日暂未新增（昨日 {prev_free}）"
        else:
            delta_desc = f"，今日有消耗（昨日 {prev_free}）"

    logger.info(f"  [{name}] userLoginRecord save OK（code 0）")
    return True, f"✅ {name}登录完成（积分发放已触发）{balance_desc}{delta_desc}"
