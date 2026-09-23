#!/usr/bin/env python3
"""ModelScope（魔搭）每日登录魔粒 — 魔粒体系激励.

原理（2026-09-23 调研，官方文档 + 社区 userscript 双重验证）:
  - 魔搭没有签到按钮；「魔粒」按每日登录发放：
    注册登录 200 魔粒/日 + 绑定阿里云账号 50 魔粒/日，
    短期魔粒 24h 有效、当日清零不累计（官方《魔粒体系说明》
    modelscope.cn/docs/aigc/aigc-quota）。
  - 发放靠「登录事件」触发：带登录 Cookie 访问魔粒页
    /magicube/usage 即可，无需点击领取
    （参考 Weidows/userscripts modelscope-magicube-checkin，实测有效）。
  - 认证: 网页登录 Cookie（整段 Cookie 头），openapi GET 无 CSRF 要求。
  - 余额: GET /openapi/v1/magicubes/balance → data.available_balance；
    Cookie 失效时返回 HTTP 401 {"code":"InvalidAuthentication"} —— 最硬失效信号。

诚实口径（吸取 baiduwp 答题分虚报的教训）:
  魔粒会被 API 推理 / AIGC 消耗，短期魔粒还会 24h 过期，
  余额差值 ≠ 当日发放额 → 只报「登录已触发 + 当前余额」，不虚构「+N 到账」。
  Cookie 寿命无公开口径，不做 L2 年龄预警，靠 401/InvalidAuthentication
  直接判定 + L3 连败追踪兜底。
"""
from __future__ import annotations

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

BASE = "https://modelscope.cn"
USAGE_URL = f"{BASE}/magicube/usage?tab=earn"
BALANCE_URL = f"{BASE}/openapi/v1/magicubes/balance"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 余额字段候选（接口文档不公开，按 userscript 与常见命名宽容解析）
_BALANCE_KEYS = ("available_balance", "availableBalance", "balance", "total", "amount")


def _load_cookie(site: dict) -> str:
    """从 MODELSCOPE_CREDENTIALS 读 {"cookie": "<整段 Cookie 头>"}；兼容纯 Cookie 串。"""
    raw = os.getenv(site.get("credentials_env", "MODELSCOPE_CREDENTIALS"), "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return str(data.get("cookie", "")).strip()
    except Exception:
        pass
    return raw  # 整段就是 Cookie 头的写法


def _extract_balance(data) -> float | None:
    """从 balance 接口响应里抠出数值余额，兼容 data 包裹与多种字段名。"""
    if not isinstance(data, dict):
        return None
    candidates = [data]
    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.insert(0, inner)
    for obj in candidates:
        for key in _BALANCE_KEYS:
            val = obj.get(key)
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str) and val.replace(".", "", 1).isdigit():
                return float(val)
    return None


def run_modelscope_checkin(site: dict, state: dict) -> tuple[bool, str]:
    name = site["name"]
    cookie = _load_cookie(site)
    if not cookie:
        return False, (
            f"❌ {name}缺少凭证 — 请配置 Secret "
            f"{site.get('credentials_env', 'MODELSCOPE_CREDENTIALS')}"
            "（{\"cookie\": \"<modelscope.cn 整段 Cookie 头>\"}）"
        )

    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Referer": f"{BASE}/",
        "Cookie": cookie,
    })

    # ---- 1. 访问魔粒页触发当日发放 ----
    try:
        r_page = s.get(USAGE_URL, timeout=30)
    except requests.RequestException as e:
        return False, f"❌ {name}网络异常: {e}"

    # ---- 2. 余额接口验证（顺带兜底触发 + 判定 Cookie 有效性）----
    try:
        r_bal = s.get(BALANCE_URL, timeout=30, headers={"Accept": "application/json"})
    except requests.RequestException as e:
        # 页面 200 就算登录动作已完成，余额接口挂了不咬死
        if r_page.status_code == 200:
            return True, f"✅ {name}登录完成（每日魔粒已触发），余额查询失败: {e}"
        return False, f"❌ {name}网络异常（页面 HTTP {r_page.status_code}）: {e}"

    if r_bal.status_code in (401, 403):
        return False, (
            f"❌ {name}Cookie 已失效 — 余额接口 HTTP {r_bal.status_code}，"
            "请重新登录 modelscope.cn 抓取整段 Cookie 更新 Secret"
        )
    try:
        bal_json = r_bal.json()
    except Exception:
        bal_json = None
    if isinstance(bal_json, dict) and bal_json.get("code") == "InvalidAuthentication":
        return False, (
            f"❌ {name}Cookie 已失效（InvalidAuthentication）— "
            "请重新登录 modelscope.cn 抓取整段 Cookie 更新 Secret"
        )

    balance = _extract_balance(bal_json)
    if balance is not None:
        msg = f"✅ {name}登录完成（每日魔粒已触发），余额 {balance:g} 魔粒（短期当日有效）"
        if r_page.status_code != 200:
            msg += f"；⚠️ 魔粒页 HTTP {r_page.status_code}，若连续多日余额无增长需人工核查"
        return True, msg

    # 余额解析失败但没报失效 → 登录态大概率还在，页面 200 即放行（诚实标注不确定性）
    if r_page.status_code == 200 and isinstance(bal_json, dict) and bal_json.get("success") is not False:
        raw = json.dumps(bal_json, ensure_ascii=False)[:160]
        logger.info(f"  [{name}] 余额字段未识别: {raw}")
        return True, f"✅ {name}登录完成（每日魔粒已触发），余额字段未识别（响应: {raw}）"

    # 页面未登录态 / 响应异常
    if r_page.status_code in (401, 403):
        return False, (
            f"❌ {name}Cookie 已失效 — 魔粒页 HTTP {r_page.status_code}，"
            "请重新登录 modelscope.cn 抓取整段 Cookie 更新 Secret"
        )
    body = ""
    if bal_json is not None:
        body = json.dumps(bal_json, ensure_ascii=False)[:200]
    else:
        body = r_bal.text[:200]
    return False, f"❌ {name}响应异常 — 页面 HTTP {r_page.status_code}，余额接口: {body}"
