#!/usr/bin/env python3
"""跨天状态追踪 — 解决「Action 显示 success 但某站点其实一直在失败」的静默失败问题。

单次运行只能看到当天结果，看不出「这个站点已经连续 5 天失败了」。
state.json 提交回仓库，让每次运行都能读到历史。

同时它承担 L1 保活职责: 每天写一次 state.json 并 commit,
仓库自然保持活跃，GitHub 60 天无活动自动禁用 schedule 的规则就不会触发。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"

# 连续失败达到该次数时，通知标题升级为紧急
ALERT_STREAK_THRESHOLD = 3


def load_state(path: str = STATE_FILE) -> dict:
    """读取状态文件；不存在或损坏时返回空结构"""
    if not os.path.exists(path):
        logger.info(f"{path} 不存在，创建新状态")
        return {"version": 1, "sites": {}, "credentials": {}, "runs": []}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("sites", {})
        data.setdefault("credentials", {})
        data.setdefault("runs", [])
        return data
    except Exception as e:
        logger.warning(f"读取 {path} 失败（{e}），重建状态")
        return {"version": 1, "sites": {}, "credentials": {}, "runs": []}


def save_state(state: dict, path: str = STATE_FILE) -> None:
    """写回状态文件"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        logger.info(f"状态已写入 {path}")
    except Exception as e:
        logger.error(f"写入 {path} 失败: {e}")


def record_results(
    state: dict,
    results: list[tuple[str, bool, str]],
    now: datetime,
) -> list[str]:
    """记录本次结果并更新连续失败计数。

    返回需要升级告警的站点文案列表。
    """
    today = now.strftime("%Y-%m-%d")
    alerts: list[str] = []
    sites_state = state.setdefault("sites", {})

    for name, success, desc in results:
        entry = sites_state.setdefault(
            name,
            {"fail_streak": 0, "last_success": None, "last_run": None, "total_runs": 0, "total_fails": 0},
        )

        entry["last_run"] = today
        entry["total_runs"] = entry.get("total_runs", 0) + 1

        if success:
            if entry.get("fail_streak", 0) > 0:
                logger.info(f"  [{name}] 已恢复正常（此前连续失败 {entry['fail_streak']} 天）")
                entry["recovered_at"] = today
            entry["fail_streak"] = 0
            entry["last_success"] = today
        else:
            entry["fail_streak"] = entry.get("fail_streak", 0) + 1
            entry["total_fails"] = entry.get("total_fails", 0) + 1
            streak = entry["fail_streak"]

            if streak >= ALERT_STREAK_THRESHOLD:
                last_ok = entry.get("last_success") or "从未成功"
                alerts.append(
                    f"🚨 {name} 已连续失败 {streak} 天（最后成功: {last_ok}）— 需要人工介入"
                )

    # 保留最近 30 次运行摘要，便于回溯
    runs = state.setdefault("runs", [])
    runs.append({
        "date": today,
        "success": sum(1 for _, s, _ in results if s),
        "fail": sum(1 for _, s, _ in results if not s),
    })
    state["runs"] = runs[-30:]
    state["last_run_at"] = now.isoformat()

    return alerts


def build_streak_summary(state: dict, results: list[tuple[str, bool, str]]) -> dict[str, str]:
    """为报告生成连续失败摘要。

    返回 {站点名: 摘要文案}——用 dict 而非 list，
    避免按顺序消费时挂错站点。
    """
    notes: dict[str, str] = {}
    sites_state = state.get("sites", {})
    for name, success, _desc in results:
        if success:
            continue
        entry = sites_state.get(name, {})
        streak = entry.get("fail_streak", 0)
        if streak > 1:
            last_ok = entry.get("last_success") or "从未成功"
            notes[name] = f"   ↳ 连续失败 {streak} 天，最后成功 {last_ok}"
    return notes
