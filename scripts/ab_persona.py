"""对照实验：为什么同一个模型在 Trae 上是「知心大姐姐」，在这里是「旅行助手刷了层温柔」。

四个变量，一次只动一个：
  A 原样            —— 14 条规则 + 22 个工具全绑
  B 去掉「条理清晰」 —— 只改第 14 条
  C 不绑工具        —— 只去掉 22 个工具 schema
  D 身份改成「默认擅长」+ 默认简洁 + 人格优先

**每个臂重复多次**：单次 A/B 会骗人。第一次跑出来 B 的分点降到 0，
第二次又回到 4——如果只跑一次，就会得出「条理清晰是列表的元凶」这个错误结论。

用法：
    python scripts/ab_persona.py            # 每臂 3 次
    python scripts/ab_persona.py --repeat 5
"""

from __future__ import annotations

import argparse
import asyncio
import re

from app.agent.graph import SYSTEM_PROMPT
from app.agent.model import build_chat_model
from app.agent.tools import ALL_TOOLS
from app.core.config import load_settings

TURNS = ["你能不能变成知心大姐姐", "我好迷茫"]

TRAVEL_WORDS = ("行程", "景点", "散心", "去哪儿走", "旅行", "出游", "目的地", "攻略")

# B：只把第 14 条的「条理清晰」拿掉
PROMPT_B = SYSTEM_PROMPT.replace(
    "14. 用中文回答，条理清晰。",
    "14. 用中文回答。",
)

# D：身份从「是」改成「默认擅长」，默认简洁，并让人格优先
PROMPT_D = (
    SYSTEM_PROMPT.replace(
        "你是旅行规划助手。你可以调用工具查询天气、检索地点、规划路线。\n",
        "你是一个中文对话助手，默认擅长旅行规划，可以调用工具查询天气、检索地点、规划路线。\n"
        "当用户只是闲聊、表达情绪，或明确要求换一种说话方式时，"
        "先按用户要的方式说话，把旅行专长放在背景里，不要主动把话题引回旅行。\n",
    )
    .replace("14. 用中文回答，条理清晰。", "14. 用中文回答。")
    .replace(
        "规则：\n",
        "默认简洁：能一句话说清就不要分点，只有用户要清单或内容确实并列时才用列表。\n规则：\n",
    )
)


def list_marks(text: str) -> int:
    """粗数分点痕迹：编号、项目符号、A/B/C 选项。"""
    return len(re.findall(r"(?m)^\s*(?:\d+[.、)]|[-•*]|[A-D][.、)])", text))


def travel_hits(text: str) -> list[str]:
    return [word for word in TRAVEL_WORDS if word in text]


async def run_arm(model, system: str, bind_tools: bool) -> str:
    """跑一轮完整对话，返回最后一条回答。"""
    chat = model.bind_tools(ALL_TOOLS) if bind_tools else model
    history: list[tuple[str, str]] = [("system", system)]
    text = ""
    for turn in TURNS:
        history.append(("user", turn))
        reply = await chat.ainvoke(history)
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        history.append(("assistant", text))
    return text


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3, help="每个臂重复次数")
    args = parser.parse_args()

    settings = load_settings()
    model = build_chat_model(settings)
    arms = [
        ("A 原样", SYSTEM_PROMPT, True),
        ("B 去条理清晰", PROMPT_B, True),
        ("C 不绑工具", SYSTEM_PROMPT, False),
        ("D 默认擅长", PROMPT_D, True),
    ]

    print(f"model={settings.model_name}  repeat={args.repeat}\n")
    header = f"{'臂':<14}{'字数':>14}{'分点':>12}{'带旅行词':>10}"
    print(header)
    print("-" * len(header.encode("gbk", errors="ignore")))

    samples: dict[str, str] = {}
    for label, prompt, bind in arms:
        lengths: list[int] = []
        marks: list[int] = []
        travel = 0
        last = ""
        for _ in range(args.repeat):
            out = await run_arm(model, prompt, bind)
            last = out
            lengths.append(len(out))
            marks.append(list_marks(out))
            travel += 1 if travel_hits(out) else 0
        samples[label] = last
        print(
            f"{label:<14}"
            f"{min(lengths)}-{max(lengths):>9}"
            f"{min(marks)}-{max(marks):>9}"
            f"{travel}/{args.repeat:>7}"
        )

    print("\n【D 的一次完整回答】")
    print(samples["D 默认擅长"])
    print("\n【A 的一次完整回答】")
    print(samples["A 原样"])


if __name__ == "__main__":
    asyncio.run(main())
