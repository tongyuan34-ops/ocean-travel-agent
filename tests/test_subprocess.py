"""采集子进程的超时与进程树清理（SPEC 4.8 / 4.12）。

真实拉起子进程来验：这条路径必须能确定性地测到，不能只靠人工观察——
MediaCrawler 会拉起 Chromium，只 kill 父进程就会堆孤儿浏览器。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import pytest

from app.adapters.mediacrawler import run_crawler_subprocess


async def test_normal_exit_captures_output():
    run = await run_crawler_subprocess(
        [sys.executable, "-c", "print('hello')"], cwd=".", timeout=60
    )
    assert run.returncode == 0
    assert run.timed_out is False
    assert "hello" in run.stdout


async def test_timeout_reports_and_returns_promptly():
    started = time.perf_counter()
    run = await run_crawler_subprocess(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=".", timeout=3
    )
    elapsed = time.perf_counter() - started

    assert run.timed_out is True
    assert "超时" in run.stderr
    assert elapsed < 25, f"超时后没有及时返回：{elapsed:.1f}s"


@pytest.mark.skipif(sys.platform == "win32", reason="进程组是 POSIX 概念，Windows 走 taskkill")
async def test_child_runs_in_its_own_process_group():
    """子进程必须独立成组，否则超时清理会误杀自己。

    回归用例：_kill_process_tree 在 POSIX 上用 killpg(getpgid(pid))。
    如果子进程没以 start_new_session 启动，它继承的就是本进程的组号，
    killpg 会把 API 服务（以及 CI 里的 pytest）连同整组一起 SIGKILL——
    表现是 job 卡死而不是报错，极难排查。
    """
    code = "import os; print(os.getpgid(0), os.getpgid(os.getppid()))"
    run = await run_crawler_subprocess([sys.executable, "-c", code], cwd=".", timeout=60)

    assert run.returncode == 0, run.stderr
    child_pgid, parent_pgid = run.stdout.split()
    assert child_pgid != parent_pgid, (
        f"子进程未独立成组（child={child_pgid} parent={parent_pgid}），超时清理会把父进程一起杀掉"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="进程树清理按 Windows taskkill 实现")
async def test_timeout_kills_descendant_processes():
    """孙进程也必须一起清掉，否则后台会留下孤儿浏览器。"""
    marker = "trae_test_sleep_marker"
    nested = (
        "import subprocess, sys, time;"
        f"subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep(60)  # {marker}']);"
        "time.sleep(60)"
    )

    before = _count_processes_with_marker(marker)
    run = await run_crawler_subprocess([sys.executable, "-c", nested], cwd=".", timeout=6)
    assert run.timed_out is True

    await asyncio.sleep(2)
    after = _count_processes_with_marker(marker)
    assert after <= before, f"仍有残留进程：before={before} after={after}"


def _count_processes_with_marker(marker: str) -> int:
    listing = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" "
            "| Select-Object -ExpandProperty CommandLine",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return sum(1 for line in listing.stdout.splitlines() if marker in line)
