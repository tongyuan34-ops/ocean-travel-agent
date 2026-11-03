"""把一份 .env 快照打进 ``app/core/_bundled_keys.py``，供出包时内置。

    python scripts/make_bundled_keys.py --dry-run        # 只列出会打包哪些项，不打印值
    python scripts/make_bundled_keys.py                  # 生成 app/core/_bundled_keys.py
    python scripts/make_bundled_keys.py --env-file prod.env

**生成物不要提交**：它带着 API key，``.gitignore`` 里已经排除。

**为什么只放白名单**

黑名单（「除了这几个都打包」）在有人往 .env 里加了一个新密钥的那天就漏了。
白名单是唯一安全的默认：不认识的项一律不打包，需要的话显式加进来。

**打包之前先做两件事**

1. 给这些 key 在服务商后台设一个**低额度**——内置 key 等于公开发布，
   任何人拿到安装包都能用，费用算你的。
2. 记住换 key 不需要发新版本：用户在 ``config.json`` 里填自己的 key 就能覆盖。

混淆说明见 ``app/core/secrets.py``：base64 + 异或只挡随手翻包的人。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / ".env"
DEFAULT_OUT = ROOT / "app" / "core" / "_bundled_keys.py"

# 白名单：让应用「装完就能用」所需的全部配置。
# 不含：REDIS_URL / CORS_ORIGINS / WEB_AUTH_* / COOKIE_DIR / MEDIACRAWLER_* /
# ALLOWED_ROOTS —— 这些和「这台机器」绑定，打包进去只会把别人的路径带过去。
INCLUDE = (
    # 大模型
    "MODEL_PROVIDER",
    "MODEL_NAME",
    "MODEL_API_KEY",
    "MODEL_BASE_URL",
    "MAX_CONTEXT_TOKENS",
    # 图片
    "IMAGE_BACKEND",
    "CUSTOM_IMAGE_ENDPOINT",
    "CUSTOM_IMAGE_TOKEN",
    "CUSTOM_IMAGE_MODEL",
    "CUSTOM_IMAGE_SIZE",
    "CUSTOM_IMAGE_RESPONSE_FORMAT",
    "CUSTOM_IMAGE_WATERMARK",
    # 视频
    "VIDEO_BACKEND",
    "ARK_API_KEY",
    "ARK_BASE_URL",
    "ARK_VIDEO_MODEL",
    "ARK_VIDEO_RATIO",
    "ARK_VIDEO_GENERATE_AUDIO",
    "ARK_VIDEO_WATERMARK",
    # 检索
    "MAP_API_KEY",
    "WEATHER_API_KEY",
    "BING_SEARCH_API_KEY",
    "LITEAPI_KEY",
    "LITEAPI_BASE_URL",
)

# 不跟 .env 走、出包时**强制**覆盖的值。
#
# 踩过一次：开发机 .env 里是 ``STORAGE_BACKEND=redis``（当时在测多进程），
# 直接打包进去，用户的机器上根本没有 Redis，会话就存不进去。这类值不能
# 「取开发机的现状」，必须「取桌面版应有的样子」。
OVERRIDES = {
    # 用户机器上没有 Redis。落盘到 exe 旁边的 data/sessions.json
    "STORAGE_BACKEND": "file",
    # 桌面版不该让会话过期——用户关掉程序明天再开，聊天记录得还在
    "SESSION_TTL_SEC": "0",
}

TEMPLATE = '''"""出包用的内置配置快照。

由 ``scripts/make_bundled_keys.py`` 生成，**不要手改**，也不要提交——
这里带着 API key。``.gitignore`` 里已经排除了这个文件。

没有这个文件时应用照常启动：``secrets.bundled()`` 会 ImportError，
然后退回 .env 和 config.json。
"""

from __future__ import annotations

import base64
import json

_SALT = {salt!r}
_PAYLOAD = {payload!r}


def keys() -> dict[str, str]:
    raw = base64.b64decode(_PAYLOAD)
    plain = bytes(byte ^ _SALT[index % len(_SALT)] for index, byte in enumerate(raw))
    return json.loads(plain.decode("utf-8"))
'''


def parse_env_file(path: Path) -> dict[str, str]:
    """只认 ``KEY=VALUE`` 行。不处理多行值和变量展开——.env 是给人写的，
    真需要复杂语法的时候该换成正式配置文件。"""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def encode(values: dict[str, str]) -> tuple[bytes, str]:
    """返回 (盐, payload)。盐每次构建随机——同一个 key 两次构建出来的密文不同。"""
    salt = os.urandom(24)
    plain = json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")
    masked = bytes(byte ^ salt[index % len(salt)] for index, byte in enumerate(plain))
    return salt, base64.b64encode(masked).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--dry-run", action="store_true", help="只列出会打包哪些项")
    args = parser.parse_args()

    if not args.env_file.is_file():
        print(f"找不到 {args.env_file}", file=sys.stderr)
        return 1

    values = parse_env_file(args.env_file)
    picked = {key: values[key] for key in INCLUDE if values.get(key)}
    missing = [key for key in INCLUDE if not values.get(key)]

    overridden = {key: forced for key, forced in OVERRIDES.items() if values.get(key, "") != forced}
    picked.update(OVERRIDES)

    if args.dry_run:
        print(f"会打包 {len(picked)} 项：")
        for key in picked:
            # 只报长度，不报值：这个输出会被人贴进 issue
            print(f"  {key:<28} {len(picked[key])} 字符")
        if overridden:
            print("\n强制覆盖（不跟 .env 走）：")
            for key, forced in overridden.items():
                print(f"  {key:<28} .env={values.get(key) or '（未配）'} → {forced}")
        if missing:
            print(f"\n没配（不会打包，运行时用默认值）：{', '.join(missing)}")
        return 0

    if not picked.get("MODEL_API_KEY"):
        print("MODEL_API_KEY 是空的：打出来的包在别人机器上跑不起来。", file=sys.stderr)
        return 1

    salt, payload = encode(picked)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(TEMPLATE.format(salt=salt, payload=payload), encoding="utf-8", newline="\n")

    print(f"已写入 {args.out}（{len(picked)} 项）")
    if overridden:
        print("强制覆盖：", "，".join(f"{key} → {forced}" for key, forced in overridden.items()))
    if missing:
        print(f"没配、运行时用默认值：{', '.join(missing)}")
    print(
        "\n提醒：内置 key 等于公开发布。去服务商后台给它设一个低额度，"
        "泄露了就换——用户在 config.json 里填自己的 key 即可覆盖，不用发新版本。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
