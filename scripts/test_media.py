"""媒体链路冒烟测试：先验证后端能不能真的出结果，再往上层接。

用法（在项目根目录执行）：

    python scripts/test_media.py caps
    python scripts/test_media.py image "海边日落，胶片质感"
    python scripts/test_media.py video "海浪拍岸，镜头缓缓推进"
    python scripts/test_media.py video "女孩回头微笑" --image-url https://example.com/frame.png

说明：
- 会按 .env 的配置真实调用上游，可能产生费用
- 视频通常要等几十秒到几分钟，脚本按配置的退避策略轮询
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许从项目根目录直接运行本脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import load_settings  # noqa: E402
from app.media.base import MediaKind, MediaRequest  # noqa: E402
from app.media.service import build_media_service  # noqa: E402


async def _run_image(service, prompt: str, style: str | None) -> None:
    task = await service.submit(
        MediaRequest(kind=MediaKind.IMAGE, prompt=prompt, style=style)
    )
    await _wait_and_report(service, task.id)


async def _run_video(
    service, prompt: str, image_url: str | None, duration: int | None
) -> None:
    task = await service.submit(
        MediaRequest(
            kind=MediaKind.VIDEO,
            prompt=prompt,
            ref_image=image_url,
            duration=duration,
        )
    )
    await _wait_and_report(service, task.id)


async def _wait_and_report(service, task_id: str) -> None:
    print(f"任务已创建：{task_id}")
    last_status = None
    while True:
        task = await service.get(task_id)
        if task is None:
            print("任务丢失（内存存储仅限当前进程）")
            return
        if task.status.value != last_status:
            print(f"  状态 → {task.status.value}")
            last_status = task.status.value
        if task.status.value in {"succeeded", "failed"}:
            break
        await asyncio.sleep(2)

    if task.status.value == "succeeded":
        print(f"成功：{task.result_url}")
    else:
        print(f"失败：{task.error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="媒体链路冒烟测试")
    parser.add_argument(
        "action", choices=["caps", "image", "video"], help="要执行的动作"
    )
    parser.add_argument("prompt", nargs="?", default="", help="生成提示词")
    parser.add_argument("--style", default=None, help="图片风格")
    parser.add_argument("--image-url", default=None, help="视频首帧参考图 URL")
    parser.add_argument("--duration", type=int, default=None, help="视频时长（秒）")
    args = parser.parse_args()

    settings = load_settings()
    service = build_media_service(settings)

    if args.action == "caps":
        print("图片后端：", service.capabilities()["image_gen"])
        print("视频后端：", service.capabilities()["video_gen"])
        reasons = service.unavailable_reasons()
        if reasons:
            print("不可用原因：")
            for key, reason in reasons.items():
                print(f"  {key}: {reason}")
        return 0

    if not args.prompt:
        parser.error("image / video 动作需要提供提示词")

    if args.action == "image":
        asyncio.run(_run_image(service, args.prompt, args.style))
    else:
        asyncio.run(
            _run_video(service, args.prompt, args.image_url, args.duration)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
