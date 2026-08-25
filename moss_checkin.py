#!/usr/bin/env python3
"""Moss API 每日登录签到 — SSO 静默重放 + 令牌轮换持久化。

原理（2026-08-26 实测逆向）:
  platform.mosi.cn 的每日 +100 积分由「登录完成（新令牌签发）」触发，
  已有会话刷新页面不发放。登录走 Mossland SSO，可用 cookie 重放:

  1. POST mossland.studio/api/v1/auth/token/refresh   (Cookie: refresh_token=...)
     → 换 1h access_token；**每次成功刷新会轮换 refresh_token（Set-Cookie）**，
       旧 token 数次复用后被拉黑（auth_token_blacklisted）。
  2. GET  platform.mosi.cn/portal-api/auth/studio/sso/start
     → HTML meta refresh 里带 authorize URL
  3. GET  mossland.studio/api/v1/auth/platform/sso/authorize?...  (Cookie: access_token=...)
     → 302 回 platform.mosi.cn/api/v1/auth/studio/sso/callback?code&state
     → 回调页 #studio_sso_session=<base64 JSON>（服务端此刻完成发券）
  4. GET  platform.mosi.cn/portal-api/me (Bearer 新令牌) 验证 + 查余额

轮换持久化（应对 1 的轮换）:
  - 活跃 token 存 state['moss_sso']['rt_blob']：AES-256-GCM 加密（密钥在
    Secret MOSS_RT_ENC_KEY），state.json 每日随仓库提交 → 跨天可用。
  - Secret MOSS_REFRESH_TOKEN 只是「种子」（首次运行或链条断裂时启用）。
  - 拿到轮换新 token 后立即落盘，再做 SSO，最大限度避免链条丢失。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

MOSSLAND_REFRESH_URL = "https://mossland.studio/api/v1/auth/token/refresh"
SSO_START_URL = "https://platform.mosi.cn/portal-api/auth/studio/sso/start"
ME_URL = "https://platform.mosi.cn/portal-api/me"
TRANSACTIONS_URL = "https://platform.mosi.cn/portal-api/maas/credits/transactions"
SUMMARY_URL = "https://platform.mosi.cn/portal-api/maas/credits/usage-summary"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------- AES-GCM（依赖 cryptography） ----------------

def _enc_key():
    import os
    raw = os.getenv("MOSS_RT_ENC_KEY", "")
    if not raw:
        return None
    try:
        key = base64.b64decode(raw)
        return key if len(key) == 32 else None
    except Exception:
        return None


def _encrypt_token(token: str, key) -> str:
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, token.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def _decrypt_token(blob: str, key) -> str | None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        raw = base64.b64decode(blob)
        return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode()
    except Exception:
        return None


def _load_active_token(site: dict, state: dict) -> tuple[str, str]:
    """返回 (token, 来源)。优先 state 里的活跃链条，其次 env 种子。"""
    blob = (state.get("moss_sso") or {}).get("rt_blob", "")
    key = _enc_key()
    if blob and key:
        tok = _decrypt_token(blob, key)
        if tok:
            return tok, "state"
    seed = site.get("refresh_token", "")
    if seed:
        return seed, "seed"
    return "", ""


def _persist_token(token: str, state: dict) -> bool:
    """轮换后立即持久化。返回是否成功。"""
    key = _enc_key()
    if not key:
        logger.warning("  [moss] 未配置 MOSS_RT_ENC_KEY，轮换 token 无法持久化（明天将回落种子→失效）")
        return False
    state.setdefault("moss_sso", {})["rt_blob"] = _encrypt_token(token, key)
    state["moss_sso"]["updated_at"] = datetime.now(
        timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    from state import save_state
    save_state(state)  # 立即落盘，SSO 再失败也不丢链条
    logger.info("  [moss] 轮换后的 refresh_token 已加密持久化")
    return True


def _extract_rotated_token(resp) -> str | None:
    """从 refresh 响应的 Set-Cookie 里取轮换后的 refresh_token。"""
    for c in resp.cookies:
        if c.name == "refresh_token" and c.value:
            return c.value
    # requests 未按 cookie jar 解析时的兜底：手解析头
    sc = resp.headers.get("Set-Cookie", "")
    m = re.search(r"refresh_token=([^;]+)", sc)
    return m.group(1) if m else None


# ---------------- 主流程 ----------------

def _fail(msg: str) -> tuple[bool, str]:
    return False, (
        f"{msg}\n"
        "💡 请在浏览器重新登录 mossland.studio（密码登录有滑块验证码），\n"
        "   然后让 ZCode 用 ego-browser 取新 refresh_token 更新 Secret"
    )


def _decode_session_fragment(fragment: str) -> dict | None:
    try:
        raw = urllib.parse.unquote(fragment)
        raw += "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(raw))
        if isinstance(data, dict) and data.get("access_token"):
            return data
    except Exception:
        pass
    return None


def _do_refresh(session, token: str):
    """返回 (access_token, rotated_refresh_token|None, resp)。"""
    r = session.post(MOSSLAND_REFRESH_URL, cookies={"refresh_token": token},
                     timeout=30)
    access_token = ""
    try:
        data = r.json()
        if data.get("code") == 0:
            access_token = data.get("data", {}).get("access_token", "")
    except Exception:
        pass
    return access_token, _extract_rotated_token(r), r


def apply_chain_token_for_health(sites: list[dict], state: dict) -> None:
    """health.py 的 JWT 预警不适合 mossland 的轮换 token：链头每次轮换都是
    「现在+7天」，remaining 永远 ≤ 阈值 7 → 天天误报。链条存在 = 每天都在自续，
    无需预警（链断了签到会自己失败）；只有链条缺失时才看种子的固定 exp。"""
    blob = (state.get("moss_sso") or {}).get("rt_blob", "")
    if not blob:
        return  # 无链条：保留种子 curl_bash，按种子 exp 预警（有意义）
    for site in sites:
        if site.get("auth_mode") == "moss_sso":
            site.pop("curl_bash", None)  # 跳过 JWT 预警


def run_moss_sso_checkin(site: dict, state: dict) -> tuple[bool, str]:
    name = site["name"]
    token, source = _load_active_token(site, state)
    if not token:
        return False, f"❌ {name}无可用凭证（state 链条与 MOSS_REFRESH_TOKEN 种子均缺失）"

    s = requests.Session()
    s.headers.update({"User-Agent": UA})

    # ---- 1. 刷新 access_token（带轮换处理） ----
    logger.info(f"[{name}] → POST mossland auth/token/refresh（token 来源: {source}）")
    try:
        access_token, rotated, r1 = _do_refresh(s, token)
    except requests.RequestException as e:
        return False, f"❌ {name}网络异常: {e}"

    if not access_token and rotated:
        # 旧 token 被拉黑但响应仍下发了新链（少见）——持久化后重试一次
        _persist_token(rotated, state)
        access_token, rotated2, r1b = _do_refresh(s, rotated)
    if not access_token and source == "state":
        # state 链条失效 → 回落种子
        seed = site.get("refresh_token", "")
        if seed and seed != token:
            logger.info(f"  [{name}] state 链条失效，回落 Secret 种子")
            access_token, rotated, r1 = _do_refresh(s, seed)
            source = "seed"
    if not access_token:
        state.get("moss_sso", {}).pop("rt_blob", None)
        return _fail(f"❌ {name}refresh 失败 HTTP {r1.status_code} — refresh_token 已过期/被拉黑")
    logger.info("  refresh OK，拿到 1h access_token")

    if rotated:
        _persist_token(rotated, state)

    # ---- 2. SSO start → authorize URL ----
    logger.info(f"[{name}] → GET sso/start")
    try:
        r2 = s.get(SSO_START_URL, timeout=30)
    except requests.RequestException as e:
        return False, f"❌ {name}网络异常: {e}"
    m = re.search(r"url=(https://mossland\.studio[^\"']+)", r2.text)
    if not m:
        return _fail(f"❌ {name}sso/start 响应异常 HTTP {r2.status_code}")
    authorize_url = m.group(1).replace("&amp;", "&")

    # ---- 3. authorize（带 access_token cookie）→ 302 → callback ----
    logger.info(f"[{name}] → GET authorize → callback")
    try:
        r3 = s.get(authorize_url, cookies={"access_token": access_token},
                   timeout=30)
    except requests.RequestException as e:
        return False, f"❌ {name}网络异常: {e}"

    if "studio_sso_session" not in r3.text:
        return _fail(f"❌ {name}SSO 未通过（回调页无会话片段）HTTP {r3.status_code}")
    m2 = re.search(r"#studio_sso_session=([^\"'\s]+)", r3.text)
    if not m2:
        return _fail(f"❌ {name}回调页会话片段解析失败")
    session_data = _decode_session_fragment(m2.group(1))
    if not session_data:
        return _fail(f"❌ {name}会话片段解码失败")
    logger.info("  SSO 回调 OK，平台令牌已签发（发券时刻）")

    # ---- 4. 验证 + 余额 ----
    headers = {"Authorization": f"Bearer {session_data['access_token']}"}
    try:
        me = s.get(ME_URL, headers=headers, timeout=30).json()
        tx = s.get(TRANSACTIONS_URL, params={"limit": 5, "offset": 0},
                   headers=headers, timeout=30).json()
        summary = s.get(SUMMARY_URL, headers=headers, timeout=30).json()
    except Exception as e:
        return True, f"✅ {name}登录成功（积分发放已触发），但查询失败: {e}"

    beijing = timezone(timedelta(hours=8))
    today = datetime.now(beijing).strftime("%Y-%m-%d")
    granted = any(
        (it.get("processed_at") or "")[:10] == today
        and it.get("direction") == "credit"
        and it.get("status") == "granted"
        for it in (tx.get("items") or [])[:5]
    )
    balance = summary.get("display_available_points")
    email = str(me.get("email", "")).replace(r"^(.).*", r"\1***")
    grant_desc = "今日 +100 已到账" if granted else "今日发放已触发（若早前登录过则已领）"
    bal_desc = f"，余额 {balance}" if balance is not None else ""
    return True, f"✅ {name}登录成功 — {grant_desc}{bal_desc}（{email}）"
