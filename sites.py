#!/usr/bin/env python3
"""站点配置 — 纯声明式，加站点只改这里。

每个站点配置块:
  name:              显示名（用于报告）
  curl_bash_env:     存 curl_bash 的环境变量名（对应 GitHub Secret）
  success_keywords:  响应中表示成功的中文/英文关键词
  already_keywords:  响应中表示"今日已签"的关键词
  enabled:           是否启用（默认 True，设为 False 可临时禁用）
"""
import os

SITES = [
    {
        "name": "海豚湾",
        "curl_bash_env": "HITUN_CURL_BASH",
        "success_keywords": ["签到成功", "成功", "获得流量", "已领取", "续命"],
        "already_keywords": ["已签到", "今日已签", "已经签到", "签到过", "续命过", "似乎已经"],
        "auth_fail_keywords": ["未登录", "未注册", "请先登录", "登录失败", "token", "授权"],
        "cf_fail_keywords": ["cloudflare", "turnstile", "captcha", "验证码", "just a moment"],
        "enabled": True,
    },
    # 已移除：忍者云（Passkey 强制验证，无法自动化）
    # 已移除：idkey（Cloudflare cf_clearance IP 绑定，GitHub Actions 无法绕过）
    {
        "name": "ListenHub-免费",
        "curl_bash_env": "LISTENHUB_FREE_CURL_BASH",
        "default_body": '{"platform":"listenhub"}',
        "success_keywords": ["success", "checkin", "签到"],
        "already_keywords": ["already", "已签到", "今日已签"],
        "auth_fail_keywords": ["unauthorized", "invalid token", "expired", "未登录"],
        "cf_fail_keywords": [],
        "enabled": True,
    },
    {
        "name": "ListenHub-会员",
        "curl_bash_env": "LISTENHUB_PRO_CURL_BASH",
        "default_body": '{"platform":"listenhub"}',
        "success_keywords": ["success", "checkin", "签到"],
        "already_keywords": ["already", "已签到", "今日已签"],
        "auth_fail_keywords": ["unauthorized", "invalid token", "expired", "未登录"],
        "cf_fail_keywords": [],
        "enabled": True,
    },
    {
        "name": "idkey",
        "curl_bash_env": "IDKEY_CURL_BASH",
        "success_keywords": ["\"status\":\"success\""],
        # idkey 的已签到返回 status=fail 且带积分数据
        "already_keywords": ["\"status\":\"fail\""],
        "auth_fail_keywords": ["unauthorized", "invalid token", "expired", "未登录"],
        "cf_fail_keywords": ["cloudflare", "just a moment"],
        "enabled": False,
    },
    {
        "name": "Moss-API",
        "auth_mode": "moss_sso",
        "refresh_token_env": "MOSS_REFRESH_TOKEN",
        # 挂到 curl_bash_env 仅为让 health.py 的 JWT 到期预警提示正确的 Secret 名
        "curl_bash_env": "MOSS_REFRESH_TOKEN",
        "success_keywords": ["登录成功"],
        "already_keywords": ["已领", "已到账"],
        "auth_fail_keywords": ["过期", "无效", "登录页"],
        "cf_fail_keywords": [],
        "enabled": True,
    },
]


def get_enabled_sites() -> list[dict]:
    """返回启用的站点列表，从环境变量读入凭证"""
    result = []
    for site in SITES:
        if not site.get("enabled", True):
            continue
        site_copy = site.copy()

        if site.get("auth_mode") == "sspanel_login":
            # 账号密码登录模式
            email = os.getenv(site["email_env"], "")
            passwd = os.getenv(site["passwd_env"], "")
            if not email or not passwd:
                continue
            site_copy["email"] = email
            site_copy["passwd"] = passwd
        elif site.get("auth_mode") == "moss_sso":
            # moss_sso：env 只是种子，state 链条也可用，因此无 env 也启用
            token = os.getenv(site["refresh_token_env"], "")
            if token:
                site_copy["refresh_token"] = token
                # 复用 health.py 的 JWT 预警：refresh_token 是 JWT
                site_copy["curl_bash"] = "Bearer " + token
        else:
            # curl_bash 回放模式
            curl_bash = os.getenv(site["curl_bash_env"], "")
            if not curl_bash:
                continue
            site_copy["curl_bash"] = curl_bash

        result.append(site_copy)
    return result


def get_disabled_reasons() -> list[str]:
    """返回未启用或未配置的站点及原因"""
    reasons = []
    for site in SITES:
        if not site.get("enabled", True):
            reasons.append(f"⏸️ {site['name']} — 已禁用")
        elif site.get("auth_mode") == "moss_sso":
            if not os.getenv(site.get("refresh_token_env", ""), ""):
                reasons.append(
                    f"ℹ️ {site['name']} — 种子 Secret {site.get('refresh_token_env')} 未配置"
                    f"（若 state 链条存在仍可运行）")
        elif site.get("auth_mode") == "sspanel_login":
            if not os.getenv(site["email_env"], "") or not os.getenv(site["passwd_env"], ""):
                reasons.append(f"⚠️ {site['name']} — Secret {site['email_env']}/{site['passwd_env']} 未配置")
        elif not os.getenv(site["curl_bash_env"], ""):
            reasons.append(f"⚠️ {site['name']} — Secret {site['curl_bash_env']} 未配置")
    return reasons
