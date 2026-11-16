#!/usr/bin/env python3
"""RunningHub 每日登录积分（福利中心）+ 免费创作额度巡检。

原理（2026-11-16 实测逆向，CN 站 www.runninghub.cn）:
  - 福利中心有两个独立池子：
      1. 每日登录积分 30 POINT（bucket=ACTIVITY，当日 24:00 清零）
      2. 免费视频 3 次 + 免费图片 3 次（half-month 周期，见 summary.freePeriod）
  - 领取走网页会话接口，**不接受 API Key**：
      POST /api/welfare/daily-gift/claim   → 领取（幂等，已领返回 data:false）
      POST /api/welfare/summary            → 复核 dailyGift.granted / freeVideo.remaining
  - API Key（RUNNINGHUB_API_KEY）打这些端点一律 412 TOKEN_INVALID——
    所以不能复用现有的 API Key，必须持有网页 token。

令牌链（关键：可全自动，无需人工定期重抓）:
  POST /uc/token/refresh (Bearer refresh_token)
    → 返回全新 access_token + refresh_token
  实测结论（2026-11-16）:
    - refresh_token 是**滑动窗口**：每次刷新发新 RT，有效期重置为 60 天。
      只要每 60 天内跑过一次，链条永续 → 不需要人肉重抓。
    - **旧 RT 可重复使用**（连刷 6 次全成功，不像 MOSS 复用数次即拉黑），
      因此无需 AES 加密轮换链，明文存 state.json 即可。
    - access_token 有效期 10 天，但每天签到都会刷新，不会撞到过期。

免费创作额度（只能网页端用，API 吃不到）:
  - 免费额度走 qc-service 私有链路（prepare→commit），且绑定 model bindingId。
  - 用 API Key 打 fee-preview → 412；用网页 token 打 → welfareFree:true。
  - 本模块**只巡检剩余次数并报告**，不代为消耗（越权调私有创作链路风险高、
    且会闯入用户的创作额度，不属于签到职责）。

Secret:
  RUNNINGHUB_REFRESH_TOKEN  首次的种子（从浏览器 localStorage 的
                            mosi/Rh-Refreshtoken 取），之后自动续期。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

BASE = "https://www.runninghub.cn"
REFRESH_URL = f"{BASE}/uc/token/refresh"
CLAIM_URL = f"{BASE}/api/welfare/daily-gift/claim"
SUMMARY_URL = f"{BASE}/api/welfare/summary"
POINT_SUMMARY_URL = f"{BASE}/uc/point/summary"
GAIN_LIST_URL = f"{BASE}/uc/point/gainList"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

STATE_KEY = "runninghub"


def _headers(token: str) -> dict:
    """网页会话请求头。缺 client/Origin/Referer 会被风控判为非浏览器。"""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "client": "WEB",
        "User-Language": "zh_CN",
        "X-RH-Lang": "zh",
        "Origin": BASE,
        "Referer": f"{BASE}/",
        "User-Agent": UA,
    }


# ---------------- 令牌链 ----------------

def _load_token(site: dict, state: dict) -> tuple[str, str]:
    """返回 (refresh_token, 来源)。state 优先，回落 env 种子。"""
    rt = (state.get(STATE_KEY) or {}).get("refresh_token", "")
    if rt:
        return rt, "state"
    seed = site.get("refresh_token", "")
    if seed:
        return seed, "seed"
    return "", ""


def _persist(access: str, refresh: str, state: dict) -> None:
    """旋转后的令牌立即落盘（随 state.json 提交回仓库）。

    RH 的 RT 可重复使用且 60 天滑动续期，故明文存放即可；
    若将来平台收紧，再参照 MOSS 的 AES-GCM 加密方案升级。
    """
    node = state.setdefault(STATE_KEY, {})
    node["refresh_token"] = refresh
    node["access_token"] = access
    node["updated_at"] = datetime.now(
        timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    try:
        from state import save_state
        save_state(state)  # 立即落盘，后续步骤失败也不丢链条
        logger.info("  [RunningHub] 轮换后的令牌已持久化")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"  [RunningHub] 令牌持久化失败: {e}")


def _refresh(session: requests.Session, refresh_token: str):
    """刷新令牌，返回 (access, refresh, resp)。失败时 access 为空串。"""
    r = session.post(REFRESH_URL, json={},
                     headers=_headers(refresh_token), timeout=30)
    access = refresh = ""
    try:
        data = r.json()
        if data.get("code") == 0:
            inner = data.get("data") or {}
            access = inner.get("access_token", "")
            refresh = inner.get("refresh_token", "")
    except Exception:  # noqa: BLE001
        pass
    return access, refresh, r


def _fail(msg: str) -> tuple[bool, str]:
    return False, (
        f"{msg}\n"
        "💡 请浏览器登录 runninghub.cn，从 localStorage 取 Rh-Refreshtoken，\n"
        "   执行 gh secret set RUNNINGHUB_REFRESH_TOKEN -R pommotion/daily-checkin"
    )


# ---------------- 主流程 ----------------

def run_runninghub_checkin(site: dict, state: dict) -> tuple[bool, str]:
    name = site["name"]
    token, source = _load_token(site, state)
    if not token:
        return _fail(f"❌ {name}无可用凭证（state 与 RUNNINGHUB_REFRESH_TOKEN 均缺失）")

    s = requests.Session()
    s.headers.update({"User-Agent": UA})

    # ---- 1. 刷新 access_token ----
    logger.info(f"[{name}] → POST /uc/token/refresh（token 来源: {source}）")
    try:
        access, rotated, r1 = _refresh(s, token)
    except requests.RequestException as e:
        return False, f"❌ {name}网络异常: {e}"

    # state 链条失效 → 回落 Secret 种子
    if not access and source == "state":
        seed = site.get("refresh_token", "")
        if seed and seed != token:
            logger.info(f"  [{name}] state 令牌失效，回落 Secret 种子")
            try:
                access, rotated, r1 = _refresh(s, seed)
            except requests.RequestException as e:
                return False, f"❌ {name}网络异常: {e}"
            source = "seed"

    if not access:
        state.get(STATE_KEY, {}).pop("refresh_token", None)
        return _fail(
            f"❌ {name}refresh 失败 HTTP {r1.status_code} — refresh_token 已失效/被拉黑")

    logger.info("  refresh OK")
    if rotated:
        _persist(access, rotated, state)

    hdr = _headers(access)

    # ---- 2. 领取每日登录积分（幂等）----
    logger.info(f"[{name}] → POST /api/welfare/daily-gift/claim")
    claim_ok = False
    claim_note = ""
    try:
        rc = s.post(CLAIM_URL, json={}, headers=hdr, timeout=30)
        if rc.status_code in (401, 403):
            return _fail(f"❌ {name}领取接口鉴权失败 HTTP {rc.status_code}")
        rc_json = rc.json()
        if rc_json.get("code") == 0:
            # data=true 表示本次真的发了；false = 今天已领过
            claim_ok = bool(rc_json.get("data"))
            claim_note = "本次领取成功" if claim_ok else "今日已领取过（幂等跳过）"
        else:
            logger.warning(f"  [{name}] claim 返回 code={rc_json.get('code')} msg={rc_json.get('msg')}")
    except requests.RequestException as e:
        return False, f"❌ {name}领取网络异常: {e}"
    except ValueError:
        logger.warning(f"  [{name}] claim 响应非 JSON，继续用 summary 复核")

    # ---- 3. summary 复核 + 免费额度巡检 ----
    try:
        rs = s.post(SUMMARY_URL, json={}, headers=hdr, timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        if claim_ok:
            return True, f"✅ {name}积分已领取（本次 +30），但复核失败: {e}"
        return False, f"❌ {name}summary 查询失败: {e}"

    if rs.get("code") != 0:
        if claim_ok:
            return True, f"✅ {name}积分已领取（本次 +30），summary code={rs.get('code')}"
        return False, f"❌ {name}summary 异常 code={rs.get('code')} msg={rs.get('msg')}"

    data = rs.get("data") or {}
    gift = data.get("dailyGift") or {}
    granted = bool(gift.get("granted"))
    amount = gift.get("amount", "?")

    fv = data.get("freeVideo") or {}
    fi = data.get("freeImage") or {}
    fv_left = fv.get("remaining", "?")
    fi_left = fi.get("remaining", "?")
    fv_total = fv.get("total", "?")
    fi_total = fi.get("total", "?")

    # ---- 4. 可选：查当日积分流水确认入账 ----
    today_points = None
    try:
        rg = s.post(GAIN_LIST_URL, json={"page": 1, "pageSize": 5},
                    headers=hdr, timeout=30).json()
        beijing = timezone(timedelta(hours=8))
        today = datetime.now(beijing).strftime("%Y-%m-%d")
        for rec in (rg.get("data") or {}).get("records", []):
            if (rec.get("sourceType") == "DAILY_GIFT"
                    and str(rec.get("gainTime", "")).startswith(today)):
                today_points = rec.get("amountPoint")
                break
    except Exception:  # noqa: BLE001
        pass  # 流水只是锦上添花，失败不影响判定

    exp = gift.get("expireTime", "")
    if exp:
        try:
            dt = datetime.fromisoformat(exp.replace("Z", "+00:00")).astimezone(
                timezone(timedelta(hours=8)))
            exp_desc = f"，{dt:%H:%M} 前有效" if dt.date() == datetime.now(
                timezone(timedelta(hours=8))).date() else ""
        except Exception:  # noqa: BLE001
            exp_desc = ""
    else:
        exp_desc = ""

    if not granted:
        return False, (
            f"❌ {name}每日积分未到账（summary granted=false）— {claim_note or '领取无响应'}")

    point_desc = f"+{today_points:g}" if isinstance(today_points, (int, float)) else f"+{amount}"
    return True, (
        f"✅ {name}登录积分 {point_desc} 已到账{exp_desc}"
        f"；免费额度 视频 {fv_left}/{fv_total} 图片 {fi_left}/{fi_total}（仅网页端可用）"
    )
