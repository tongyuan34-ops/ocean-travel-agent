"""真机验证：一句话触发「说做了却没做」，看自愈有没有真的跑第二趟。

只做观测，不做断言——模型行为不是确定性的，这里的价值是把 SSE 原始事件
打出来，人工确认 retry 有没有出现、第二趟有没有真调 generate_image。
"""

from __future__ import annotations

import json
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
DEVICE = "verify-" + uuid.uuid4().hex[:12]


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "帮我生成三张湄洲岛旅行图"
    # 每次请求单开连接：本机 uvicorn 在 Windows 上复用 keep-alive 时，
    # 同一连接上的第二个请求会拿到 404（FastAPI 默认 detail:Not Found）。
    # 前端走 serve.py 反代、每次新建连接，不受影响；这里照做即可。
    with httpx.Client(base_url=BASE, headers={"X-Device-ID": DEVICE}, timeout=30) as c:
        r = c.post("/chat/sessions", json={})
        r.raise_for_status()
        sid = r.json()["session_id"]
    print(f"session={sid}")

    kinds: list[str] = []
    answer = ""
    with (
        httpx.Client(base_url=BASE, headers={"X-Device-ID": DEVICE}, timeout=180) as c,
        c.stream("POST", f"/chat/sessions/{sid}/messages", json={"message": text}) as resp,
    ):
        if resp.status_code != 200:
            body = resp.read().decode("utf-8", "replace")[:500]
            print(f"HTTP {resp.status_code}: {body}")
            return 1
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            kinds.append(ev.get("type", "?"))
            if ev.get("type") == "token":
                answer += ev.get("content", "")
            elif ev.get("type") == "tool_call":
                print(f"  [tool_call] {ev.get('tool')}")
            elif ev.get("type") == "tool_result":
                summary = ev.get("summary", "")[:120]
                print(f"  [tool_result] {ev.get('status')} {summary}")
            elif ev.get("type") == "retry":
                print(f"  [RETRY] {ev}")
            elif ev.get("type") == "end":
                print(f"  [end] finish={ev.get('finish_reason')}")
                print(f"  [end] warnings={ev.get('warnings')}")
                print(f"  [end] latency={ev.get('latency_ms')}ms")
            elif ev.get("type") == "error":
                print(f"  [ERROR] {ev}")

    print("\n事件序列：", kinds)
    print(f"retry 次数：{kinds.count('retry')}")
    print(f"tool_call 次数：{kinds.count('tool_call')}")
    print("\n回答：\n" + answer[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
