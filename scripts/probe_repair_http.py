"""临时探针：用假图跑真 HTTP，逼出一次自愈，验证 SSE 里真的有 retry。

为什么不用真模型：真模型这一轮已经会老老实实调工具，逼不出「说做了没做」。
假图能稳定复现那一类故障，而请求仍然走完整的 FastAPI + SSE + 事件日志链路，
比在 pytest 里断言更接近线上。

用法：
    .venv\\Scripts\\python.exe scripts/probe_repair_http.py   # 监听 8001
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from app.services import chat as chat_module  # noqa: E402

CLAIM = "三张图都提交生成好了，等前端轮询出图就会显示。"


class _Scripted:
    """第一趟只说不做，第二趟真调工具。"""

    def __init__(self) -> None:
        self.calls = 0

    async def astream_events(
        self, _input: Any, version: str | None = None, config: Any = None
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        if self.calls == 1:
            yield {
                "event": "on_chain_end",
                "data": {"output": {"messages": [AIMessage(content=CLAIM)]}},
            }
            return
        yield {
            "event": "on_tool_start",
            "name": "generate_image",
            "run_id": "img1",
            "data": {"input": {"prompt": "湄洲岛日落"}},
        }
        yield {
            "event": "on_tool_end",
            "name": "generate_image",
            "run_id": "img1",
            "data": {"output": "已创建生图任务\ntask_id：probe-task-0001\n状态：running"},
        }
        yield {
            "event": "on_chain_end",
            "data": {"output": {"messages": [AIMessage(content="已经提交好了，稍等。")]}},
        }


_graph = _Scripted()
chat_module.build_agent_graph = lambda _s: (_graph, "native")


if __name__ == "__main__":
    import uvicorn

    from app.main import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=8001, log_level="warning")
