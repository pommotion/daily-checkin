#!/usr/bin/env python3
"""凭证健康检查 — 在凭证过期【之前】预警，而不是过期后才发现连续失败。

两类凭证的检查策略不同:

  1. JWT (ListenHub Bearer token)
     → 可直接解出 payload 里的 exp 字段，精确算出剩余天数

  2. 不透明 Cookie (cf_clearance 等)
     → 无法从内容解析有效期，改用「首次见到该值的日期」推算
       state.json 记录每个凭证的指纹 + 首见日期，
       指纹变化 = 用户更新了凭证 = 重置计时
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# 各类凭证的经验寿命（天）
CF_CLEARANCE_LIFETIME_DAYS = 30
DEFAULT_OPAQUE_LIFETIME_DAYS = 60

# 剩余天数小于该值时开始预警
WARN_THRESHOLD_DAYS = 7


def _b64url_decode(seg: str) -> bytes:
    """解 base64url，补齐 padding"""
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def extract_jwt_exp(curl_bash: str) -> datetime | None:
    """从 curl_bash 里找 JWT 并解出过期时间。找不到返回 None。

    JWT 特征: 三段 base64url，用 . 分隔，第一段解出来是 {"alg":...}
    """
    # 宽松匹配所有类 JWT 字符串（Authorization header 或 Cookie 里都可能）
    # header/payload 至少 8 字符；签名段长度不限（部分实现签名很短，卡 {8,} 会漏匹配）
    candidates = re.findall(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+", curl_bash)

    for token in candidates:
        try:
            header_seg, payload_seg, _sig = token.split(".")
            header = json.loads(_b64url_decode(header_seg))
            if "alg" not in header:
                continue
            payload = json.loads(_b64url_decode(payload_seg))
            exp = payload.get("exp")
            if not exp:
                continue
            return datetime.fromtimestamp(int(exp), tz=timezone.utc)
        except Exception:
            continue

    return None


def extract_cf_clearance(curl_bash: str) -> str | None:
    """从 curl_bash 里提取 cf_clearance 的值"""
    m = re.search(r"cf_clearance=([^;'\"\s]+)", curl_bash)
    return m.group(1) if m else None


def fingerprint(value: str) -> str:
    """凭证指纹 — 只存 hash，绝不把凭证明文写进 state.json"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def check_site_credential(site: dict, state: dict, today: datetime) -> str | None:
    """检查单个站点的凭证健康度。

    返回预警文案；健康则返回 None。
    会就地更新 state 里该站点的凭证指纹与首见日期。
    """
    curl_bash = site.get("curl_bash", "")
    if not curl_bash:
        return None

    name = site["name"]
    creds = state.setdefault("credentials", {})
    entry = creds.setdefault(name, {})

    warnings: list[str] = []

    # ---- 1. JWT: 精确过期时间 ----
    exp_dt = extract_jwt_exp(curl_bash)
    if exp_dt:
        remaining = (exp_dt - today).days
        entry["jwt_exp"] = exp_dt.isoformat()
        entry["jwt_days_left"] = remaining

        if remaining < 0:
            warnings.append(
                f"🚨 {name} JWT 已于 {exp_dt.strftime('%Y-%m-%d')} 过期 "
                f"— 立即更新 {site['curl_bash_env']}"
            )
        elif remaining <= WARN_THRESHOLD_DAYS:
            warnings.append(
                f"⚠️ {name} JWT 还有 {remaining} 天过期"
                f"（{exp_dt.strftime('%Y-%m-%d')}）— 建议尽快更新 {site['curl_bash_env']}"
            )

    # ---- 2. cf_clearance: 按首见日期推算 ----
    cf_value = extract_cf_clearance(curl_bash)
    if cf_value:
        fp = fingerprint(cf_value)
        stored_fp = entry.get("cf_fingerprint")

        if stored_fp != fp:
            # 凭证换新了（或第一次记录）→ 重置计时
            entry["cf_fingerprint"] = fp
            entry["cf_first_seen"] = today.strftime("%Y-%m-%d")
            logger.info(f"  [{name}] cf_clearance 已更新，重置有效期计时")
        else:
            first_seen = entry.get("cf_first_seen")
            if first_seen:
                try:
                    seen_dt = datetime.strptime(first_seen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    age = (today - seen_dt).days
                    remaining = CF_CLEARANCE_LIFETIME_DAYS - age
                    entry["cf_days_left"] = remaining

                    if remaining <= 0:
                        warnings.append(
                            f"🚨 {name} cf_clearance 已用了 {age} 天"
                            f"（经验寿命 {CF_CLEARANCE_LIFETIME_DAYS} 天）— 建议重新抓包"
                        )
                    elif remaining <= WARN_THRESHOLD_DAYS:
                        warnings.append(
                            f"⚠️ {name} cf_clearance 预计还有 {remaining} 天失效 — 建议尽快重新抓包"
                        )
                except ValueError:
                    pass

    return "\n".join(warnings) if warnings else None


def check_all(sites: list[dict], state: dict) -> list[str]:
    """检查所有站点凭证，返回预警列表"""
    today = datetime.now(timezone.utc)
    warnings = []
    for site in sites:
        w = check_site_credential(site, state, today)
        if w:
            warnings.append(w)
    return warnings
