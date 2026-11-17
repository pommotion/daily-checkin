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
        # 2026-10-31 停用：Cloudflare 收紧 cf_clearance IP 绑定校验，GitHub runner IP
        # 与签发 IP 不一致必被拦（连续失败 11 天实证）。已迁移本地 launchd：
        # ~/AgentWork/hitun-local-checkin/（每天本地 09:12/18:12 各一次，走 Clash 7890）
        "enabled": False,
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
        # 2026-09-03 用户决定停用（JWT 已过期，不再续）
        "enabled": False,
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
        # 2026-11-16 改版：不再有签到按钮，改为「账号密码登录即触发每日 50 积分发放」
        # 旧 moss_sso 静默重放链路已死（域名迁 moss.cn、refresh 端点 404）
        "auth_mode": "moss_sso",
        "credentials_env": "MOSS_CREDENTIALS",
        "success_keywords": ["登录完成"],
        "already_keywords": ["已发放", "持平"],
        "auth_fail_keywords": ["登录被拒", "未下发", "401"],
        "cf_fail_keywords": [],
        "enabled": True,
    },
    {
        "name": "LibTV",
        "auth_mode": "libtv",
        # LIBTV_CREDENTIALS = JSON {"token": "<usertoken cookie>", "webid": "<webid cookie>"}
        "credentials_env": "LIBTV_CREDENTIALS",
        "enabled": True,
    },
    {
        "name": "WorkBuddy",
        "auth_mode": "workbuddy",
        # WORKBUDDY_CREDENTIALS = {"accessToken","refreshToken","expiresAt","domain",
        # "uid","nickname"}（扁平或 CPA 插件嵌套格式均可）；轮换链存 state 加密 blob
        "credentials_env": "WORKBUDDY_CREDENTIALS",
        # 2026-11-17 停用归档：WorkBuddy 已在本机 Magpie 网关实现多槽自动续期与每日签到，停用云端重复任务防刷新链互抢
        "enabled": False,
    },
    {
        "name": "ModelScope",
        "auth_mode": "modelscope",
        # MODELSCOPE_CREDENTIALS = {"cookie": "<modelscope.cn 整段 Cookie 头>"}
        # 每日登录 200 魔粒（绑阿里云 +50），访问魔粒页即触发，无领取按钮
        "credentials_env": "MODELSCOPE_CREDENTIALS",
        "enabled": True,
    },
    {
        "name": "RunningHub",
        "auth_mode": "runninghub",
        # RUNNINGHUB_REFRESH_TOKEN = 浏览器 localStorage 的 Rh-Refreshtoken（种子）
        # 每日登录 +30 积分（当日有效）；refresh_token 60 天滑动续期，自动滚链无需重抓
        # ⚠️ 必须用网页 token，RUNNINGHUB_API_KEY 打福利接口一律 412
        "refresh_token_env": "RUNNINGHUB_REFRESH_TOKEN",
        # 挂到 curl_bash_env 仅为让 health.py 的 JWT 到期预警提示正确的 Secret 名
        "curl_bash_env": "RUNNINGHUB_REFRESH_TOKEN",
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
            # 2026-11-16 改版：改为账号密码登录（登录即发放），不再用 refresh_token
            creds = os.getenv(site.get("credentials_env", "MOSS_CREDENTIALS"), "")
            if not creds:
                continue
            site_copy["credentials"] = creds
        elif site.get("auth_mode") == "runninghub":
            # runninghub：env 只是种子，state 里的滑动窗口令牌也可用，故无 env 也启用
            token = os.getenv(site.get("refresh_token_env", ""), "")
            if token:
                site_copy["refresh_token"] = token
        elif site.get("auth_mode") == "libtv" or site.get("auth_mode") == "modelscope":
            creds = os.getenv(site["credentials_env"], "")
            if not creds:
                continue
            site_copy["credentials"] = creds
        elif site.get("auth_mode") == "workbuddy":
            # workbuddy：env 只是种子，state 轮换链也可用，因此无 env 也启用（同 moss）
            pass
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
        elif site.get("auth_mode") == "libtv":
            if not os.getenv(site["credentials_env"], ""):
                reasons.append(f"⚠️ {site['name']} — Secret {site['credentials_env']} 未配置（token/webid JSON）")
        elif site.get("auth_mode") == "modelscope":
            if not os.getenv(site["credentials_env"], ""):
                reasons.append(f"⚠️ {site['name']} — Secret {site['credentials_env']} 未配置（网页 Cookie JSON）")
        elif site.get("auth_mode") == "workbuddy":
            if not os.getenv(site["credentials_env"], ""):
                reasons.append(
                    f"ℹ️ {site['name']} — 种子 Secret {site['credentials_env']} 未配置"
                    f"（若 state 轮换链存在仍可运行）")
        elif site.get("auth_mode") == "runninghub":
            if not os.getenv(site.get("refresh_token_env", ""), ""):
                reasons.append(
                    f"ℹ️ {site['name']} — 种子 Secret {site.get('refresh_token_env')} 未配置"
                    f"（若 state 令牌链存在仍可运行）")
        elif site.get("auth_mode") == "sspanel_login":
            if not os.getenv(site["email_env"], "") or not os.getenv(site["passwd_env"], ""):
                reasons.append(f"⚠️ {site['name']} — Secret {site['email_env']}/{site['passwd_env']} 未配置")
        elif not os.getenv(site["curl_bash_env"], ""):
            reasons.append(f"⚠️ {site['name']} — Secret {site['curl_bash_env']} 未配置")
    return reasons
