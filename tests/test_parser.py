#!/usr/bin/env python3
"""本地测试 — 用模拟 curl_bash 验证 curl_parser 解析逻辑"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from curl_parser import parse_curl

# 测试用例：模拟四站的 curl 格式

test_cases = {
    "hitun (SSPanel-Cookie)": """curl 'https://hitun.io/user/checkin' -X 'POST' -H 'accept: application/json' -b 'PHPSESSID=abc123; cf_clearance=xyz789' -H 'x-requested-with: XMLHttpRequest'""",
    
    "忍者云 (SSPanel-Cookie)": """curl 'https://renzhe.cloud/user/checkin' -X 'POST' -H 'accept: application/json' -b 'cf_clearance=aaa; uid=106971; email=violin%40gmail.com; key=bbb' -H 'content-length: 0' -H 'x-requested-with: XMLHttpRequest'""",
    
    "ListenHub (Bearer JWT)": """curl 'https://listenhub.ai/api/listenhub/v1/checkin' -H 'accept: */*' -H 'authorization: Bearer eyJhbGc.eyJzdWIiOiJ0ZXN0In0.signature' -H 'content-type: application/json' -H 'x-listenhub-client-id: 7pTkoq*B4vmV8' --data-raw '{"platform":"listenhub"}'""",
}

print("=" * 60)
for name, curl_str in test_cases.items():
    print(f"\n--- {name} ---")
    try:
        spec = parse_curl(curl_str)
        print(f"  Method: {spec['method']}")
        print(f"  URL:    {spec['url']}")
        print(f"  Cookies: {list(spec['cookies'].keys())}")
        print(f"  Headers: {list(spec['headers'].keys())}")
        print(f"  Body:   {spec['body']}")
        
        # 验证关键字段
        assert spec["url"], "URL 缺失"
        assert spec["method"] in ("POST", "GET"), f"Method 异常: {spec['method']}"
        print(f"  ✅ 解析正常")
    except Exception as e:
        print(f"  ❌ 解析失败: {e}")

print("\n" + "=" * 60)
print("测试完成")
