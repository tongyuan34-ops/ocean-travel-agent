"""把 ``data/sessions.json``（file 后端）迁移到 SQLite。

幂等：``INSERT OR REPLACE``，重复跑不会产生重复记录。

应用启动时也会做同样的事（首次切到 sqlite 且 ``sessions.db`` 不存在时自动导入），
这个脚本用于「库已经存在、但想再从 JSON 补一次」或者迁移到自定义路径：

    .\\.venv\\Scripts\\python.exe scripts\\migrate_sessions.py
    .\\.venv\\Scripts\\python.exe scripts\\migrate_sessions.py --source data/sessions.json --target data/sessions.db
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 直接从仓库根目录跑脚本时也能 import app
    sys.path.insert(0, str(ROOT))

from app.storage import import_legacy_sessions  # noqa: E402
from app.storage.file import read_file  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="会话数据：JSON 文件 → SQLite")
    parser.add_argument("--source", default=str(ROOT / "data" / "sessions.json"))
    parser.add_argument("--target", default=str(ROOT / "data" / "sessions.db"))
    args = parser.parse_args()

    if not Path(args.source).exists():
        raise SystemExit(f"找不到 {args.source}，先用 STORAGE_BACKEND=file 跑过再说")

    # 读一遍只为统计：导入本身在 import_legacy_sessions 里
    sessions = read_file(Path(args.source))
    count = import_legacy_sessions(Path(args.source), Path(args.target))
    by_tenant = Counter(session.tenant for session in sessions)

    print(f"已迁移 {count} 条会话 → {args.target}")
    for tenant, number in sorted(by_tenant.items(), key=lambda item: -item[1]):
        print(f"  {tenant}: {number}")
    print("原 JSON 文件保留未动，确认无误后可自行删除")


if __name__ == "__main__":
    main()
