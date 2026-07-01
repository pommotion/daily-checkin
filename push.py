#!/usr/bin/env python3
"""统一推送模块 — 支持 Telegram / PushPlus / WxPusher / Server 酱。

复用自 hitun-checkin-action，配置一次全局共享。
"""
import json
import logging
import time

import requests

logger = logging.getLogger(__name__)


class PushNotification:
    def __init__(self):
        self.headers = {"Content-Type": "application/json"}

    # -------- Telegram --------
    def push_telegram(self, content: str, token: str, chat_id: str) -> bool:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for attempt in range(3):
            try:
                # Telegram 消息上限 4096 字符，超长截断
                if len(content) > 4000:
                    content = content[:3990] + "\n...(截断)"
                resp = requests.post(
                    url,
                    data=json.dumps({"chat_id": chat_id, "text": content, "parse_mode": "Markdown"}),
                    headers=self.headers,
                    timeout=10,
                )
                resp.raise_for_status()
                logger.info(f"Telegram 推送成功")
                return True
            except Exception as exc:
                logger.error(f"Telegram 失败 attempt={attempt}: {exc}")
                time.sleep(2)
        return False

    # -------- PushPlus --------
    def push_pushplus(self, content: str, token: str, is_success: bool) -> bool:
        url = "https://www.pushplus.plus/send"
        title = f"每日签到-{'全部成功' if is_success else '有失败'}"
        for attempt in range(3):
            try:
                resp = requests.post(
                    url,
                    data=json.dumps({"token": token, "title": title, "content": content}),
                    headers=self.headers,
                    timeout=10,
                )
                resp.raise_for_status()
                logger.info(f"PushPlus 推送成功")
                return True
            except Exception as exc:
                logger.error(f"PushPlus 失败 attempt={attempt}: {exc}")
                time.sleep(2)
        return False

    # -------- WxPusher --------
    def push_wxpusher(self, content: str, spt: str) -> bool:
        try:
            app_token, uid = spt.split("|", 1)
        except ValueError:
            logger.error("WXPUSHER_SPT 格式错误，应为 appToken|uid")
            return False
        url = f"https://wxpusher.zjiecode.com/api/send/message/{app_token}/{uid}"
        for attempt in range(3):
            try:
                resp = requests.post(
                    url,
                    data=json.dumps({"content": content}),
                    headers=self.headers,
                    timeout=10,
                )
                resp.raise_for_status()
                logger.info(f"WxPusher 推送成功")
                return True
            except Exception as exc:
                logger.error(f"WxPusher 失败 attempt={attempt}: {exc}")
                time.sleep(2)
        return False

    # -------- Server 酱 --------
    def push_serverchan(self, content: str, spt: str, is_success: bool) -> bool:
        url = f"https://sctapi.ftqq.com/{spt}.send"
        title = f"每日签到-{'全部成功' if is_success else '有失败'}"
        for attempt in range(3):
            try:
                resp = requests.post(
                    url,
                    data=json.dumps({"title": title, "desp": content}),
                    headers=self.headers,
                    timeout=10,
                )
                resp.raise_for_status()
                logger.info(f"Server 酱推送成功")
                return True
            except Exception as exc:
                logger.error(f"Server 酱失败 attempt={attempt}: {exc}")
                time.sleep(2)
        return False


def push(content: str, method: str, is_success: bool = True) -> bool:
    """统一入口"""
    if not method:
        logger.warning("未配置推送渠道，跳过推送。")
        return False

    notifier = PushNotification()
    method = str(method).lower().strip()

    import os
    if method == "telegram":
        return notifier.push_telegram(
            content,
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("TELEGRAM_CHAT_ID", ""),
        )
    if method == "pushplus":
        return notifier.push_pushplus(content, os.getenv("PUSHPLUS_TOKEN", ""), is_success)
    if method == "wxpusher":
        return notifier.push_wxpusher(content, os.getenv("WXPUSHER_SPT", ""))
    if method == "serverchan":
        return notifier.push_serverchan(content, os.getenv("SERVERCHAN_SPT", ""), is_success)

    logger.warning(f"未知推送方式: {method}")
    return False
