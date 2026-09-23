#!/usr/bin/env python3
"""腾讯 CodeBuddy（WorkBuddy）每日签到 — CN 单账号。

移植自 Maquer/workbuddy-checkin（其逻辑源自 CPA workbuddy 插件
billing.go / keepalive.go），改用 requests 并接入本仓库的 state 链条。

API（{code, msg, data} 信封，code != 0 为业务错误）:
  POST /v2/billing/meter/checkin-activity-status   签到状态（fallback checkin-status）
  POST /v2/billing/meter/daily-checkin             执行签到
  POST /v2/billing/meter/get-user-resource         积分套餐包汇总
  POST /v2/plugin/auth/token/refresh               token 刷新（refreshToken 会轮换！）
区域: CN https://www.codebuddy.cn（支持签到）；Global workbuddy.ai（不支持）
会话失效标记: "12153" / "Offline user session not found" → 需重新登录

凭证: Secret WORKBUDDY_CREDENTIALS（扁平或嵌套 JSON，含 accessToken/
refreshToken/expiresAt/domain/uid/nickname）。刷新后的新 token 用
MOSS_RT_ENC_KEY 加密存 state['workbuddy']['cred_blob'] 随仓库提交——
与 moss 轮换链同构；Secret 只作链条断裂时的兜底种子。
Actions 场景下若不持久化轮换，刷新一次后旧 refreshToken 即死——
这正是原版 Actions 部署的坑，本实现规避之。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

BASE_CN = "https://www.codebuddy.cn"
BASE_GLOBAL = "https://www.workbuddy.ai"
REFRESH_CN = f"{BASE_CN}/v2/plugin/auth/token/refresh"
REFRESH_GLOBAL = f"{BASE_GLOBAL}/v2/plugin/auth/token/refresh"

EP_CHECKIN_STATUS = "/v2/billing/meter/checkin-activity-status"
EP_CHECKIN_STATUS_FALLBACK = "/v2/billing/meter/checkin-status"
EP_DAILY_CHECKIN = "/v2/billing/meter/daily-checkin"
EP_USER_RESOURCE = "/v2/billing/meter/get-user-resource"

SESSION_DEAD_MARKERS = ["Offline user session not found", "12153"]
TOKEN_REFRESH_MARGIN = 5 * 24 * 3600  # 过期前 5 天就刷新（防服务端提前撤销）
UA = "WorkBuddyCheckin/1.0 (daily-checkin integration)"


# ---- 凭证装载（state 链条优先，Secret 兜底） ----

def _seed_credentials(site: dict) -> dict:
    raw = os.getenv(site.get("credentials_env", "WORKBUDDY_CREDENTIALS"), "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    # 兼容嵌套（CPA 插件导出）与扁平两种格式
    auth = data.get("auth") or {}
    acct = data.get("account") or {}
    return {
        "accessToken": auth.get("accessToken") or data.get("accessToken", ""),
        "refreshToken": auth.get("refreshToken") or data.get("refreshToken", ""),
        "expiresAt": int(auth.get("expiresAt") or data.get("expiresAt") or 0),
        "domain": auth.get("domain") or data.get("domain", "codebuddy.cn"),
        "uid": acct.get("uid") or data.get("uid", ""),
        "nickname": acct.get("nickname") or data.get("nickname", ""),
        "enterpriseId": acct.get("enterpriseId") or data.get("enterpriseId", ""),
        "disabled": bool(data.get("disabled", False)),
    }


def _load_active(site: dict, state: dict) -> tuple[dict, str]:
    """返回 (auth_data, 来源)。state 加密链优先。"""
    from moss_checkin import _enc_key, _decrypt_token

    blob = (state.get("workbuddy") or {}).get("cred_blob", "")
    key = _enc_key()
    if blob and key:
        try:
            data = json.loads(_decrypt_token(blob, key) or "")
            if data.get("accessToken"):
                return data, "state"
        except Exception:
            pass
    seed = _seed_credentials(site)
    if seed.get("accessToken"):
        return seed, "seed"
    return {}, ""


def _persist(auth_data: dict, state: dict) -> None:
    from moss_checkin import _enc_key, _encrypt_token

    key = _enc_key()
    if not key:
        logger.warning("  [workbuddy] 未配置 MOSS_RT_ENC_KEY，轮换 token 无法持久化（下次将回落种子）")
        return
    state.setdefault("workbuddy", {})["cred_blob"] = _encrypt_token(
        json.dumps(auth_data, ensure_ascii=False), key)
    state["workbuddy"]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    from state import save_state

    save_state(state)  # 立即落盘，签到再失败也不丢链条


def _is_global(auth_data: dict) -> bool:
    return "workbuddy.ai" in (auth_data.get("domain") or "")


def _is_session_dead(err: str) -> bool:
    return any(m in err for m in SESSION_DEAD_MARKERS)


def _base(auth_data: dict) -> str:
    return BASE_GLOBAL if _is_global(auth_data) else BASE_CN


def _headers(auth_data: dict) -> dict:
    h = {
        "Authorization": f"Bearer {auth_data.get('accessToken', '')}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    if auth_data.get("uid"):
        h["X-User-Id"] = auth_data["uid"]
    if auth_data.get("enterpriseId"):
        h["X-Enterprise-Id"] = h["X-Tenant-Id"] = auth_data["enterpriseId"]
    if auth_data.get("domain"):
        h["X-Domain"] = auth_data["domain"]
    return h


def _post_json(s: requests.Session, url: str, body: dict, headers: dict):
    """返回 (data|None, status, err|None)。code != 0 视为业务错误。"""
    try:
        r = s.post(url, json=body, headers=headers, timeout=30)
    except requests.RequestException as e:
        return None, 0, str(e)
    try:
        env = r.json()
    except Exception:
        env = None
    if r.status_code >= 400 or not isinstance(env, dict):
        return None, r.status_code, r.text[:150]
    if env.get("code", 0) != 0:
        return env, r.status_code, f"code={env.get('code')} msg={env.get('msg', '')}"
    return env.get("data") if env.get("data") is not None else env, r.status_code, None


# ---- token 刷新 ----

def _refresh(s: requests.Session, auth_data: dict, state: dict) -> tuple[bool, str]:
    rt = auth_data.get("refreshToken", "")
    if not rt:
        return False, "无 refreshToken，无法刷新"
    url = REFRESH_GLOBAL if _is_global(auth_data) else REFRESH_CN
    headers = {"X-Refresh-Token": rt, "X-Auth-Refresh-Source": "workbuddy",
               "Content-Type": "application/json", "Accept": "application/json"}
    if auth_data.get("enterpriseId"):
        headers["X-Enterprise-Id"] = auth_data["enterpriseId"]
    data, status, err = _post_json(s, url, {}, headers)
    if err:
        if status == 401 and _is_session_dead(err):
            return False, "会话已失效 (12153)，需重新登录抓取凭证"
        return False, f"刷新失败 (HTTP {status}): {err}"
    if not (data or {}).get("accessToken"):
        return False, "刷新响应缺少 accessToken"
    auth_data["accessToken"] = data["accessToken"]
    if data.get("refreshToken"):
        auth_data["refreshToken"] = data["refreshToken"]
    if data.get("domain"):
        auth_data["domain"] = data["domain"]
    expires_in = data.get("expiresIn", 0)
    if expires_in:
        auth_data["expiresAt"] = int(time.time()) + int(expires_in)
    _persist(auth_data, state)
    return True, "Token 已刷新并持久化"


def _ensure_fresh(s: requests.Session, auth_data: dict, state: dict) -> None:
    exp = auth_data.get("expiresAt", 0)
    if exp and time.time() + TOKEN_REFRESH_MARGIN > exp:
        ok, msg = _refresh(s, auth_data, state)
        logger.info(f"  [workbuddy] 预刷新: {msg}")


# ---- 签到状态 ----

def _norm_status(data) -> dict:
    d = data if isinstance(data, dict) else {}

    def b(*keys):
        return any(bool(d.get(k)) for k in keys)

    def n(*keys):
        for k in keys:
            v = d.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return {
        "active": b("active", "Active"),
        "today_checked_in": b("today_checked_in", "todayCheckedIn"),
        "streak_days": n("streak_days", "streakDays"),
        "daily_credit": n("daily_credit", "dailyCredit"),
        "today_credit": n("today_credit", "todayCredit"),
        "total_credits": n("total_credits", "totalCredits"),
    }


def _fetch_status(s: requests.Session, auth_data: dict):
    headers = _headers(auth_data)
    for ep in (EP_CHECKIN_STATUS, EP_CHECKIN_STATUS_FALLBACK):
        data, status, err = _post_json(s, _base(auth_data) + ep, {}, headers)
        if err is None:
            return _norm_status(data), None
        if status == 401:
            return None, f"鉴权失败 (401): {err}"
    return None, err or "签到状态查询失败"


def _fetch_credits(s: requests.Session, auth_data: dict) -> int | None:
    """套餐包剩余积分合计。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = {
        "PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now, "PackageEndTimeRangeEnd": "2126-12-31 23:59:59",
    }
    data, status, err = _post_json(s, _base(auth_data) + EP_USER_RESOURCE, body,
                                   _headers(auth_data))
    if err or not isinstance(data, dict):
        return None
    resp = data.get("Response", data) if isinstance(data, dict) else {}
    accounts = (resp.get("Data") or {}).get("Accounts", []) if isinstance(resp, dict) else []
    total = 0
    for pkg in accounts if isinstance(accounts, list) else []:
        if isinstance(pkg, dict):
            total += int(pkg.get("RemainAmount") or pkg.get("remainAmount") or 0)
    return total


# ---- 主流程 ----

def run_workbuddy_checkin(site: dict, state: dict) -> tuple[bool, str]:
    name = site["name"]
    auth, source = _load_active(site, state)
    if not auth:
        return False, (f"❌ {name}缺少凭证 — 请配置 Secret "
                       f"{site.get('credentials_env', 'WORKBUDDY_CREDENTIALS')}"
                       f"（accessToken/refreshToken JSON）")
    if auth.get("disabled"):
        return False, f"❌ {name}会话已失效（12153）— 需重新登录抓取凭证更新 Secret"
    if _is_global(auth):
        return True, f"⏭️ {name}国际版账号不支持签到（可领一次性 trial）"

    s = requests.Session()
    _ensure_fresh(s, auth, state)

    def refresh_and_retry():
        ok, msg = _refresh(s, auth, state)
        logger.info(f"  [{name}] 401 重试刷新: {msg}")
        return ok

    # ---- 状态 ----
    st, err = _fetch_status(s, auth)
    if err:
        if "401" in err and not _is_session_dead(err) and refresh_and_retry():
            st, err = _fetch_status(s, auth)
    if err:
        dead = _is_session_dead(err)
        if dead:
            auth["disabled"] = True
            _persist(auth, state)
        return False, f"❌ {name}{'会话已失效（12153）— 需重新登录' if dead else f'签到状态查询失败: {err}'}"

    nick = f"（{auth.get('nickname')}）" if auth.get("nickname") else ""
    if st.get("today_checked_in"):
        credits = _fetch_credits(s, auth)
        bal = f"，余额 {credits}" if credits is not None else ""
        return True, f"✅ {name}{nick}今日已签到（连续 {st.get('streak_days')} 天）{bal}"
    if not st.get("active"):
        return True, f"⏭️ {name}签到活动未开启，跳过"

    # ---- 执行签到 ----
    data, status, err = _post_json(s, _base(auth) + EP_DAILY_CHECKIN, {}, _headers(auth))
    if err:
        if "401" in err and not _is_session_dead(err) and refresh_and_retry():
            data, status, err = _post_json(s, _base(auth) + EP_DAILY_CHECKIN, {},
                                           _headers(auth))
    if err:
        if "已签" in err or "already" in err.lower():
            return True, f"✅ {name}{nick}今日已签到（上游确认）"
        dead = _is_session_dead(err)
        if dead:
            auth["disabled"] = True
            _persist(auth, state)
        return False, f"❌ {name}{'会话已失效（12153）— 需重新登录' if dead else f'签到失败: {err}'}"

    gained = st.get("daily_credit") or 0
    credits = _fetch_credits(s, auth)
    bal = f"，余额 {credits}" if credits is not None else ""
    gain = f"+{gained}" if gained else "完成"
    return True, f"✅ {name}{nick}签到成功 {gain}（连续 {st.get('streak_days') + 1} 天）{bal}"
