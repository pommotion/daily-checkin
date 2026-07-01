#!/usr/bin/env python3
"""curl bash 解析器 — 从浏览器复制的 curl 命令中提取请求规格。

复用自 hitun-checkin-action，支持 -H / -b / --data-raw 等常见格式。
"""
import re
from typing import Optional


def parse_curl(curl_str: str) -> dict:
    """解析 curl bash 字符串，提取 method / url / headers / cookies / body。

    兼容两种 cookie 写法：
      - -H 'Cookie: xxx'
      - -b 'xxx'
    """
    if not curl_str or not curl_str.strip():
        raise ValueError("curl bash 字符串为空")

    # 1. method（缺省 POST）
    method_match = re.search(r"-X\s+['\"]?([A-Z]+)['\"]?", curl_str, re.I)
    method = method_match.group(1).upper() if method_match else "POST"

    # 2. url
    url_match = re.search(
        r"(https?://[^'\"\s]+)",
        curl_str,
        re.I,
    )
    if not url_match:
        raise ValueError("无法从 curl bash 中提取 URL")
    url = url_match.group(1)

    # 3. headers（引号配对）
    headers: dict[str, str] = {}
    for m in re.finditer(r"-H\s+", curl_str):
        rest = curl_str[m.end():]
        if not rest:
            continue
        first = rest[0]
        if first not in ("'", '"'):
            continue
        end = rest.find(first, 1)
        if end < 0:
            continue
        header_str = rest[1:end]
        colon = header_str.find(":")
        if colon < 0:
            continue
        key = header_str[:colon].strip()
        val = header_str[colon + 1:].strip()
        if key.lower() == "cookie":
            continue
        headers[key] = val

    # 4. cookies：优先 -b，回退 -H 'Cookie:'
    cookies: dict[str, str] = {}
    cookie_str = ""
    b_match = re.search(r"-b\s+['\"]([^'\"]+)['\"]", curl_str)
    if b_match:
        cookie_str = b_match.group(1)
    else:
        c_match = re.search(r"-H\s+['\"]Cookie:\s*([^'\"]+)['\"]", curl_str, re.I)
        if c_match:
            cookie_str = c_match.group(1)

    if cookie_str:
        for item in cookie_str.split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                cookies[k.strip()] = v.strip()

    # 5. body
    body: Optional[str] = None
    body_match = re.search(r"--data(?:-raw|-binary|-urlencode)?\s+", curl_str, re.I)
    if body_match:
        rest = curl_str[body_match.end():]
        if rest:
            first = rest[0]
            if first in ("'", '"'):
                end = rest.find(first, 1)
                body = rest[1:end] if end > 0 else rest[1:]
            else:
                body = rest.split("\n", 1)[0].rstrip("\\").strip()

    # 6. 补全关键 header
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    if not any(k.lower() == "accept" for k in headers):
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    if not any(k.lower() == "x-requested-with" for k in headers):
        headers["X-Requested-With"] = "XMLHttpRequest"

    return {
        "method": method,
        "url": url,
        "headers": headers,
        "cookies": cookies,
        "body": body,
    }
