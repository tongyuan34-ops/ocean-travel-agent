"""错误码的前后端契约。

后端 `CODE_TO_HTTP` 里每加一个码，前端 `ERROR_TEXT` 就得跟一句人话——漏了的话
用户看到的是后端抛上来的原文，像这样：

    处理失败，请稍后重试 · 处理失败：Error code: 403 - {'error': {'code':
    'AccountOverdueError', ...}}

这句话既没说清是什么问题，又给了个「重试」，而欠费重试一百次也还是欠费。
这条用例把「两边对齐」变成可执行的检查，而不是靠改代码时想起来。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.errors import CODE_TO_HTTP

APP_JS = Path(__file__).resolve().parent.parent / "web" / "app.js"

# 只在客户端产生、后端永远不会发的码
_CLIENT_ONLY = {"NETWORK", "NETWORK_TIMEOUT"}


def _js_block(name: str) -> str:
    """取出 `var NAME = { ... };` 的花括号内容。"""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index(f"var {name} = {{")
    end = source.index("\n  };", start)
    return source[start:end]


def _js_keys(name: str) -> set[str]:
    return set(re.findall(r"^\s{4}([A-Z][A-Z0-9_]*):", _js_block(name), re.MULTILINE))


def test_every_backend_code_has_frontend_text() -> None:
    """后端能发出的每个码，前端都要有中文文案。"""
    texts = _js_keys("ERROR_TEXT")
    missing = sorted(set(CODE_TO_HTTP) - texts)
    assert not missing, f"前端 ERROR_TEXT 缺这些码：{missing}"


def test_frontend_does_not_invent_backend_codes() -> None:
    """前端也不该凭空造码：拼错的码会让文案静默退化成后端原文。"""
    texts = _js_keys("ERROR_TEXT")
    extra = sorted(texts - set(CODE_TO_HTTP) - _CLIENT_ONLY)
    assert not extra, f"前端有后端不存在的码（多半是拼错）：{extra}"


def test_no_retry_codes_are_real_codes() -> None:
    """NO_RETRY 里写错的码等于没写：按钮照样会挂出来。"""
    texts = _js_keys("ERROR_TEXT")
    no_retry = _js_keys("NO_RETRY")
    assert no_retry <= texts, f"NO_RETRY 里有拼错的码：{sorted(no_retry - texts)}"


def test_retry_hint_matches_button_visibility() -> None:
    """「可重试」的文案和「不展示重试按钮」的清单不能互相矛盾。

    两句都写在同一个函数里（normalizeError 取文案、appendErrorCard 决定按钮），
    谁改一边忘了另一边，用户就会看到「请稍后重试」却没有按钮，或者反过来。
    """
    block = _js_block("ERROR_TEXT")
    no_retry = _js_keys("NO_RETRY")
    for code in no_retry:
        line = next((ln for ln in block.splitlines() if f"{code}:" in ln), "")
        assert "重试" not in line.replace("重试无效", ""), (
            f"{code} 在 NO_RETRY 里（不展示按钮），文案却还写着「重试」：{line.strip()}"
        )
