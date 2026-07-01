#!/usr/bin/env python3
"""每日签到主入口 — 遍历所有已配置站点，执行签到，汇总报告，推送通知。

架构:
  curl_bash 回放模式 → 解析 → 发请求 → 关键词判定 → 汇总

支持任意数量的站点，加站点只需在 sites.py 加配置 + 在 GitHub 加 Secret。
"""
import json
import logging
import sys
from datetime import datetime, timezone, timedelta

import requests

from curl_parser import parse_curl
from log_utils import setup_logging
from push import push
from sites import get_enabled_sites, get_disabled_reasons

logger = logging.getLogger(__name__)


def classify(text: str, status: int, site: dict) -> tuple[str, bool]:
    """根据响应文本/状态码分类：(结果描述, 是否成功)"""
    text_lower = text.lower()

    if status >= 500:
        return (f"❌ 服务器异常 HTTP {status}\n响应: {text[:200]}", False)

    # 302/301 重定向 — 通常是被踢到登录页
    if status in (301, 302, 303, 307, 308):
        return (
            f"❌ {site['name']} Cookie/Token 失效 — HTTP {status}\n"
            f"💡 请重新登录后抓包更新 {site['curl_bash_env']}",
            False,
        )

    # 尝试 JSON 解析
    ret, msg = None, text
    try:
        data = json.loads(text)
        ret = data.get("ret")
        msg = data.get("msg", "") or data.get("message", "") or text
    except Exception:
        pass

    # SSPanel 系（hitun/忍者云）: ret=1 为成功
    if ret == 1:
        return (f"✅ {site['name']}签到成功 — {msg}", True)

    # ListenHub 系: JSON 带 success/error 字段
    try:
        data = json.loads(text)
        if data.get("success") or data.get("ok"):
            detail = data.get("data", {})
            if isinstance(detail, dict):
                msg = detail.get("message", "") or detail.get("msg", "") or str(detail)[:100]
            return (f"✅ {site['name']}签到成功 — {msg}", True)
        if data.get("error"):
            err_msg = data.get("error", "")
            if isinstance(err_msg, dict):
                err_msg = err_msg.get("message", str(err_msg))
            msg = str(err_msg)[:200]
    except Exception:
        pass

    # 关键词匹配
    success_kw = site.get("success_keywords", [])
    already_kw = site.get("already_keywords", [])
    auth_kw = site.get("auth_fail_keywords", [])
    cf_kw = site.get("cf_fail_keywords", [])

    if any(k in msg for k in already_kw) or any(k.lower() in text_lower for k in already_kw):
        return (f"✅ {site['name']}今日已签到 — {msg}", True)

    if any(k.lower() in text_lower for k in cf_kw):
        return (
            f"❌ {site['name']} Cloudflare 拦截 — {text[:200]}\n"
            f"💡 可能需要更新 Cookie 中的 cf_clearance",
            False,
        )

    if any(k in msg for k in auth_kw) or any(k.lower() in text_lower for k in auth_kw):
        return (
            f"❌ {site['name']} Cookie/Token 失效 — {msg}\n"
            f"💡 请重新登录后抓包更新 {site['curl_bash_env']}",
            False,
        )

    if any(k in msg for k in success_kw):
        return (f"✅ {site['name']}签到成功 — {msg}", True)

    if status == 403:
        return (f"❌ {site['name']} HTTP 403 — {text[:200]}", False)

    if ret == 0:
        return (f"❌ {site['name']} ret=0 — {msg}", False)

    return (f"⚠️ {site['name']}未知响应\nHTTP {status}\nret={ret}\nmsg={msg}\n原始: {text[:300]}", False)


def run_site_checkin(site: dict) -> tuple[bool, str]:
    """执行单个站点的签到"""
    try:
        spec = parse_curl(site["curl_bash"])
    except Exception as e:
        return False, f"❌ {site['name']}解析 curl 失败: {e}"

    logger.info(f"[{site['name']}] → {spec['method']} {spec['url']}")
    logger.info(f"  Cookie: {len(spec['cookies'])} 个 | Header: {len(spec['headers'])} 个")

    session = requests.Session()
    session.headers.update(spec["headers"])
    session.cookies.update(spec["cookies"])

    try:
        if spec["method"] == "GET":
            resp = session.get(spec["url"], timeout=30)
        else:
            resp = session.request(
                spec["method"],
                spec["url"],
                data=spec["body"],
                timeout=30,
                allow_redirects=False,
            )
    except requests.RequestException as e:
        return False, f"❌ {site['name']}网络异常: {e}"

    # 重定向时记录 Location，方便诊断
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location", "(无 Location)")
        logger.warning(f"  [{site['name']}] HTTP {resp.status_code} → {location}")

    desc, success = classify(resp.text.strip(), resp.status_code, site)
    if success:
        logger.info(desc)
    else:
        logger.error(desc)

    return success, desc


def main() -> int:
    setup_logging()

    beijing_tz = timezone(timedelta(hours=8))
    now_str = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

    logger.info("=" * 60)
    logger.info(f"📅 每日签到启动 — {now_str}")
    logger.info("=" * 60)

    sites = get_enabled_sites()
    disabled = get_disabled_reasons()

    if not sites and not disabled:
        logger.error("没有可执行的签到任务")
        return 1

    results: list[tuple[str, bool, str]] = []

    for site in sites:
        logger.info(f"\n--- [{site['name']}] ---")
        try:
            success, desc = run_site_checkin(site)
        except Exception as e:
            success, desc = False, f"❌ {site['name']}执行异常: {e}"
            logger.exception(f"{site['name']} 异常")
        results.append((site["name"], success, desc))

    # ---- 汇总报告 ----
    success_count = sum(1 for _, s, _ in results if s)
    fail_count = len(results) - success_count
    all_success = fail_count == 0

    report_lines = [
        f"📅 每日签到报告",
        f"🕐 {now_str}",
        f"",
    ]

    for name, success, desc in results:
        emoji = "✅" if success else "❌"
        # 只取描述的第一行
        first_line = desc.split("\n")[0]
        report_lines.append(f"{emoji} {first_line}")

    if disabled:
        report_lines.append("")
        for d in disabled:
            report_lines.append(d)

    report_lines.append("")
    report_lines.append(f"📊 汇总: ✅ 成功 {success_count} | ❌ 失败 {fail_count}")

    report = "\n".join(report_lines)
    logger.info(f"\n{'=' * 60}\n{report}\n{'=' * 60}")

    # ---- 推送 ----
    import os
    push_method = os.getenv("PUSH_METHOD", "")
    if push_method:
        logger.info(f"推送渠道: {push_method}")
        push(report, push_method, is_success=all_success)
    else:
        logger.info("未配置推送渠道，跳过推送")

    # 失败不影响整体退出码（避免 Action 标红导致用户恐慌）
    # 但如果有配置站点全部缺失（0 站点），返回 1
    return 0 if len(results) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
