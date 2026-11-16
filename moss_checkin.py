#!/usr/bin/env python3
"""Moss 每日登录奖励 — 账号密码直登 + 钱包发放验证。

原理（2026-11-16 实测逆向，取代 2026-08-26 的 SSO 静默重放方案）:

  旧链路已全部失效：
    - platform.mosi.cn 域名迁移至 platform.moss.cn
    - mossland.studio/api/v1/auth/token/refresh → 404
    - 平台已**取消签到按钮**，改为「登录动作本身触发发放」

  现行可自动化链路（全部 2026-11-16 实测通过）:

    1. POST https://platform.moss.cn/portal-api/auth/account/login
       body: {"kind":"email","account":<邮箱>,"password":<密码>}
       → {"is_new_user": false}
       → Set-Cookie:
            moss_studio_prod_access_token  (Max-Age 1209600 = 14 天)
            moss_studio_prod_session_scope (Max-Age 1814400 = 21 天)

    2. GET  https://create.moss.cn/api/v1/wallet   (带上面的 cookie)
       → data.credit_type_balances[credit_type=bonus].grant_sources[]
         其中 grant_source == "log_in_daily_0811" 
         grant_source_desc == "每日登录奖励"
         remaining_credit 为赠送积分余额（5000 = 50.00 积分）

  关键结论:
    - 每日奖励 50 积分，**登录那一刻服务端签发**，不需要点任何按钮
    - 同一天重复登录是幂等的，不会叠加（实测两次登录 total 不变）
    - 因此每天跑一次本脚本，即为「每日签到」

  验证方式（诚实报告，不谎报到账）:
    - 拿不到「今日是否新增」的流水接口（wallet/transactions 等均 404），
      故只能读 grant_sources 里的累计余额并记录到 state，
      与上次运行对比：增加了 → 报「+N 已到账」；持平 → 报「今日已发放，未重复计发」。
    - 这样既不谎报，也能在连续多日持平时暴露异常。

  凭证:
    MOSS_CREDENTIALS = {"email": "...", "password": "..."}（JSON，支持数组多账号）
    不再需要 MOSS_REFRESH_TOKEN / MOSS_RT_ENC_KEY（旧 SSO 链路的产物）。
"""
from __future__ import annotations

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)


# ---------------- AES-GCM（WorkBuddy 复用，依赖 cryptography） ----------------
# 说明：WorkBuddy 的 state 加密沿用了这里的两个函数与 MOSS_RT_ENC_KEY，
# 虽然 Moss 自己已改为账号密码登录、不再需要加密，但这两个函数必须保留，
# 否则 workbuddy_checkin.py 的 import 会失败（2026-11-16 线上踩过）。

def _enc_key():
    raw = os.getenv("MOSS_RT_ENC_KEY", "")
    if not raw:
        return None
    try:
        import base64
        key = base64.b64decode(raw)
        return key if len(key) == 32 else None
    except Exception:
        return None


def _encrypt_token(token: str, key) -> str:
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, token.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def _decrypt_token(blob: str, key) -> str | None:
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        raw = base64.b64decode(blob)
        return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode()
    except Exception:
        return None

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

LOGIN_URL = "https://platform.moss.cn/portal-api/auth/account/login"
WALLET_URL = "https://create.moss.cn/api/v1/wallet"
DAILY_GRANT_SOURCE = "log_in_daily_0811"

# credit 以「1/100 积分」为单位存储：5000 == 50.00 积分
CREDIT_UNIT = 100


def _load_accounts(site: dict) -> list[dict]:
    """从 MOSS_CREDENTIALS 读单账号或多账号，返回 [{email, password}]。"""
    raw = os.getenv(site.get("credentials_env", "MOSS_CREDENTIALS"), "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"  [Moss] MOSS_CREDENTIALS JSON 解析失败: {e}")
        return []
    items = data if isinstance(data, list) else [data]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        email = (it.get("email") or it.get("account") or "").strip()
        password = (it.get("password") or "").strip()
        if email and password:
            out.append({"email": email, "password": password})
    return out


def _mask(email: str) -> str:
    """脱敏邮箱，避免日志泄露账号。"""
    if "@" not in email:
        return email[:2] + "***"
    local, domain = email.split("@", 1)
    keep = local[:1]
    return f"{keep}***@{domain}"


def _fetch_daily_bonus(session: requests.Session) -> tuple[int | None, str]:
    """读钱包里「每日登录奖励」的累计余额，返回 (credit 整数, 原始描述)。"""
    r = session.get(WALLET_URL, timeout=30, headers={"Accept": "application/json"})
    if r.status_code == 401:
        return None, "钱包接口 401（登录态未生效）"
    if r.status_code != 200:
        return None, f"钱包接口 HTTP {r.status_code}"
    try:
        body = r.json()
    except Exception:
        return None, f"钱包响应非 JSON: {r.text[:120]}"

    data = body.get("data") or {}
    for bucket in data.get("credit_type_balances") or []:
        for src in bucket.get("grant_sources") or []:
            if src.get("grant_source") == DAILY_GRANT_SOURCE:
                return int(src.get("remaining_credit") or 0), src.get("grant_source_desc", "")
    # 没有该来源条目 = 从未发放过
    return 0, "无每日登录奖励记录"


def _run_one(site: dict, state: dict, acct: dict, idx: int, total: int) -> tuple[bool, str]:
    display = site["name"] if total == 1 else f"{site['name']}[{idx + 1}]"
    masked = _mask(acct["email"])

    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://platform.moss.cn",
        "Referer": "https://platform.moss.cn/",
    })

    # ---- 1. 账号密码登录（这一步即触发每日登录奖励的签发）----
    try:
        r = s.post(
            LOGIN_URL,
            data=json.dumps({"kind": "email", "account": acct["email"], "password": acct["password"]}),
            timeout=30,
        )
    except requests.RequestException as e:
        return False, f"❌ {display}网络异常: {e}"

    if r.status_code == 429:
        return False, f"❌ {display}登录被限流 HTTP 429（登录过频，明日重试即可）"
    if r.status_code != 200:
        return False, f"❌ {display}登录失败 HTTP {r.status_code}: {r.text[:160]}"

    try:
        login_body = r.json()
    except Exception:
        return False, f"❌ {display}登录响应非 JSON: {r.text[:160]}"

    # 密码错误会返回 error 字段（实测 code/message 形式）
    if isinstance(login_body, dict) and (login_body.get("code") or login_body.get("error")):
        msg = login_body.get("message") or login_body.get("msg") or json.dumps(login_body, ensure_ascii=False)
        return False, f"❌ {display}登录被拒: {str(msg)[:160]}"

    has_token = any(
        c.name == "moss_studio_prod_access_token" and c.value
        for c in s.cookies
    )
    if not has_token:
        return False, (
            f"❌ {display}登录未下发 moss_studio_prod_access_token"
            f"（响应: {json.dumps(login_body, ensure_ascii=False)[:140]}）"
        )

    # ---- 2. 读钱包，确认每日登录奖励累计余额 ----
    credit, desc = _fetch_daily_bonus(s)
    if credit is None:
        return False, f"❌ {display}登录成功但{desc}"

    points = credit / CREDIT_UNIT

    # ---- 3. 与上次运行对比，诚实判定是否新增 ----
    key = f"moss_daily_{masked}"
    prev = (state.get("moss_daily") or {}).get(key)
    prev_credit = prev.get("credit") if isinstance(prev, dict) else None

    if prev_credit is None:
        verdict = f"✅ {display}登录完成，每日登录奖励累计 {points:g} 积分（首次记录基线）"
    elif credit > prev_credit:
        delta = (credit - prev_credit) / CREDIT_UNIT
        verdict = f"✅ {display}登录完成，每日登录奖励 +{delta:g} 积分已到账（累计 {points:g}）"
    elif credit < prev_credit:
        verdict = (
            f"✅ {display}登录完成，累计 {points:g} 积分"
            f"（较上次减少，说明期间有消耗；发放来源: {desc}）"
        )
    else:
        verdict = (
            f"⚠️ {display}登录完成，但每日登录奖励与上次持平（{points:g} 积分）"
            " — 可能今日已发放过，或发放逻辑变更，连续 3 天持平需人工核查"
        )

    # 写回基线
    st = state.setdefault("moss_daily", {})
    st[key] = {
        "credit": credit,
        "points": round(points, 2),
        "grant_source": DAILY_GRANT_SOURCE,
        "grant_source_desc": desc,
    }
    return True, verdict


def run_moss_sso_checkin(site: dict, state: dict) -> tuple[bool, str]:
    """Moss 每日登录奖励（保留旧函数名，main.py 无需改动）。"""
    accounts = _load_accounts(site)
    if not accounts:
        return False, (
            f"❌ {site['name']}缺少凭证 — 请配置 Secret "
            f"{site.get('credentials_env', 'MOSS_CREDENTIALS')}"
            '（{"email": "...", "password": "..."}，多账号传 JSON 数组）'
        )

    if len(accounts) == 1:
        return _run_one(site, state, accounts[0], 0, 1)

    lines, ok_all = [], True
    for i, acct in enumerate(accounts):
        ok, msg = _run_one(site, state, acct, i, len(accounts))
        lines.append(msg)
        ok_all = ok_all and ok
    return ok_all, "\n".join(lines)


def apply_chain_token_for_health(sites, state: dict) -> None:
    """兼容旧接口：新方案用账号密码，不再有可续期的 JWT，故不做 JWT 到期预警。

    保留空实现是为了 main.py / health.py 的导入与调用不报错。
    参数兼容列表与单个 site（旧版传的是 sites 列表）。
    """
    return None
