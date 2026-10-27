"""跨进程验证：两个独立的 uvicorn 进程（8001 / 8002）共用一份 Redis。

单进程测试覆盖不到的两件事在这里验：

1. 在 8001 发消息、看到两个 token 就去 8002 用 Last-Event-ID 续传，
   两个进程同时读同一条流，都要能读到底；
2. 在 8001 生成途中去 8002 点停止，生成端要停下来且不落库。

两个进程各持一份内存状态，除了 Redis 没有任何共享——和 ``--workers 2`` 等价，
但端口不同，能确定性地把请求打到指定进程上。

**踩过的坑（都写在这里，因为下次还会踩）**：

- 不要用「读到两个事件就 break 出 with 块」来模拟断线。httpcore 在关闭未读完的
  响应时会先把剩余数据读干净（为了复用连接），于是「断线」其实一直连着，
  取消请求要等整轮生成结束才发出去——测出来的失败是客户端的，不是服务端的。
  所以这里用后台线程发起第二个请求，主线程继续读，和真实前端的行为一致。
- 后台线程不要复用主线程那个正在流式读取的 `httpx.Client`：实测同一个 client
  上并发发请求会被拖到几十秒后。各自一个 client 就正常。
"""

from __future__ import annotations

import json
import threading
import time

import httpx

A = "http://127.0.0.1:8001/api/v1"
B = "http://127.0.0.1:8002/api/v1"
# 本机代理会拦 127.0.0.1，显式绕开
CLIENT = httpx.Client(trust_env=False, timeout=180.0)


def _frames(lines):
    """逐行产出事件。**必须是生成器**：早先写成「先收集成 list 再返回」，
    结果整个响应读完才进循环体，两个用例都退化成「流结束后才动作」——
    断言照样绿，但测的根本不是生成中的场景。"""
    for line in lines:
        if line.startswith("data: "):
            yield json.loads(line[6:])


def _new_session(base: str) -> str:
    return CLIENT.post(f"{base}/chat/sessions").json()["session_id"]


def _types(frames: list[dict]) -> list[str]:
    return [frame["type"] for frame in frames]


def test_resume_on_other_process() -> None:
    """A 进程生成中，B 进程用 Last-Event-ID 接着推。"""
    session = _new_session(A)
    resumed: list[dict] = []
    started = threading.Event()

    def resume_on_b(stream_id: str) -> None:
        started.set()
        with (
            httpx.Client(trust_env=False, timeout=180.0) as client,
            client.stream(
                "GET", f"{B}/chat/streams/{stream_id}", headers={"Last-Event-ID": "2"}
            ) as response,
        ):
            assert response.status_code == 200, response.status_code
            for frame in _frames(response.iter_lines()):
                resumed.append(frame)
                if frame["type"] in ("end", "error"):
                    break

    thread = threading.Thread(target=resume_on_b, args=("",), daemon=True)
    got: list[dict] = []
    with CLIENT.stream(
        "POST",
        f"{A}/chat/sessions/{session}/messages",
        json={"message": "用一句话介绍杭州西湖，不要用工具。", "attachments": []},
    ) as response:
        assert response.status_code == 200, response.status_code
        stream_id = response.headers["x-stream-id"]
        print(f"[A] stream={stream_id}")
        for frame in _frames(response.iter_lines()):
            got.append(frame)
            if len(got) == 2:
                # 此刻 A 还在生成，B 从第 2 条之后开始读
                thread = threading.Thread(target=resume_on_b, args=(stream_id,), daemon=True)
                thread.start()
                started.wait(5)

    thread.join(timeout=60)
    print(f"[A] 完整收到 {len(got)} 个事件，末尾={_types(got)[-1]}")
    print(f"[B] 续传收到 {len(resumed)} 个事件：{_types(resumed)[:3]}...{_types(resumed)[-1:]}")

    assert _types(got)[-1] == "end"
    assert resumed, "另一个进程没续上——共享存储没生效"
    assert _types(resumed)[0] == "token", "续传应该从第 3 条（token）开始"
    assert _types(resumed)[-1] == "end", f"续传没有收到 end：{resumed[-1]}"
    # B 从第 3 条开始读，必然比 A 少前两条
    assert len(resumed) == len(got) - 2, f"A={len(got)} B={len(resumed)}"


def test_cancel_from_other_process() -> None:
    """A 进程生成中，B 进程点停止：生成端必须停，且不落库半截回答。"""
    session = _new_session(A)
    outcome: dict = {}

    def cancel_on_b(stream_id: str) -> None:
        time.sleep(0.5)  # 让 A 先真的开始吐字
        with httpx.Client(trust_env=False, timeout=60.0) as client:
            outcome["cancel"] = client.post(f"{B}/chat/streams/{stream_id}/cancel").json()

    thread = threading.Thread(target=cancel_on_b, args=("",), daemon=True)
    frames: list[dict] = []
    with CLIENT.stream(
        "POST",
        f"{A}/chat/sessions/{session}/messages",
        json={
            "message": "写一篇 2000 字的三亚游记，分十段、每段带小标题，直接开始写，不要反问我。",
            "attachments": [],
        },
    ) as response:
        stream_id = response.headers["x-stream-id"]
        print(f"[A] stream={stream_id}")
        for frame in _frames(response.iter_lines()):
            frames.append(frame)
            if len(frames) == 1:
                thread = threading.Thread(target=cancel_on_b, args=(stream_id,), daemon=True)
                thread.start()

    thread.join(timeout=60)
    print(f"[B] cancel -> {outcome.get('cancel')}")
    print(f"[A] 流在 {len(frames)} 个事件后结束，末尾={_types(frames)[-1]}")
    assert outcome.get("cancel") == {"cancelled": True}
    assert _types(frames)[-1] != "end", "取消后不该发 end（那不是正常完成）"

    roles = [
        item["role"] for item in CLIENT.get(f"{A}/chat/sessions/{session}/messages").json()["items"]
    ]
    print(f"[A] 会话历史角色：{roles}")
    assert "assistant" not in roles, "取消后仍把半截回答写进了历史"


if __name__ == "__main__":
    for case in (test_resume_on_other_process, test_cancel_from_other_process):
        print(f"\n=== {case.__name__} ===")
        case()
        print("OK")
