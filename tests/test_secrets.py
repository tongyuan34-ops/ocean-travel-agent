"""key 的三层解析链：环境变量 > config.json > 打包内置。

这一层最怕的不是「读不到」，是「读错了却没人知道」——用户报「用不了」，
你没法判断他跑的是自己的 key 还是内置的。所以每条用例都同时断言**来源**，
不只断言值。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from scripts import make_bundled_keys

from app.core import config, secrets


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path: Path):
    """每个用例都在自己的目录里跑，且不继承宿主的 key 缓存。

    ``bundled`` 默认打空：仓库里可能真有一份出包用的 ``_bundled_keys.py``
    （构建机上就有），不隔离的话「没配置」这类用例的结果会取决于
    开发机有没有跑过生成脚本——同一个测试在两个人机器上一红一绿。
    要测内置分支的用例自己再 patch 一次。
    """
    secrets.reset()
    monkeypatch.setattr(secrets, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(secrets, "bundled", lambda: {})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    yield
    secrets.reset()


def _write_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_env_wins_over_everything(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MODEL_API_KEY", "from-env")
    _write_config(tmp_path / "config.json", {"MODEL_API_KEY": "from-config"})
    monkeypatch.setattr(secrets, "bundled", lambda: {"MODEL_API_KEY": "from-bundle"})

    assert secrets.resolve("MODEL_API_KEY") == ("from-env", secrets.ENV)


def test_config_wins_over_bundled(monkeypatch, tmp_path: Path):
    """用户的逃生口：内置额度用完了，填一个自己的就能继续用，不用等新版本。"""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    _write_config(tmp_path / "config.json", {"MODEL_API_KEY": "from-config"})
    monkeypatch.setattr(secrets, "bundled", lambda: {"MODEL_API_KEY": "from-bundle"})

    assert secrets.resolve("MODEL_API_KEY") == ("from-config", secrets.CONFIG)


def test_bundled_is_the_last_resort(monkeypatch, tmp_path: Path):
    """桌面版「装完就能用」靠的就是这一层。"""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "bundled", lambda: {"MODEL_API_KEY": "from-bundle"})

    assert secrets.resolve("MODEL_API_KEY") == ("from-bundle", secrets.BUNDLED)


def test_empty_config_value_falls_through(monkeypatch, tmp_path: Path):
    """``"MODEL_API_KEY": ""`` 的意思是「我没填」，不是「故意清空」。"""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    _write_config(tmp_path / "config.json", {"MODEL_API_KEY": ""})
    monkeypatch.setattr(secrets, "bundled", lambda: {"MODEL_API_KEY": "from-bundle"})

    assert secrets.resolve("MODEL_API_KEY") == ("from-bundle", secrets.BUNDLED)


def test_nothing_configured_reports_empty_source(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "bundled", lambda: {})

    assert secrets.resolve("MODEL_API_KEY") == ("", "")


def test_project_root_config_beats_home_config(monkeypatch, tmp_path: Path):
    """绿色版（exe 旁边放 config.json）要能盖过用户目录里的那份。"""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    _write_config(tmp_path / "config.json", {"MODEL_API_KEY": "portable"})
    _write_config(
        tmp_path / "home" / ".ocean-travel-agent" / "config.json", {"MODEL_API_KEY": "home"}
    )

    assert secrets.value("MODEL_API_KEY") == "portable"


def test_home_config_is_used_when_project_root_has_none(monkeypatch, tmp_path: Path):
    """exe 装在 Program Files 时那层目录写不进去，用户只能放用户目录。"""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    _write_config(
        tmp_path / "home" / ".ocean-travel-agent" / "config.json", {"MODEL_API_KEY": "home"}
    )

    assert secrets.value("MODEL_API_KEY") == "home"


def test_broken_config_does_not_stop_startup(monkeypatch, tmp_path: Path):
    """配置文件写坏了（多半是手改漏了个逗号）不该让程序起不来。"""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    (tmp_path / "config.json").write_text("{ 这不是 json", encoding="utf-8")
    monkeypatch.setattr(secrets, "bundled", lambda: {"MODEL_API_KEY": "from-bundle"})

    assert secrets.value("MODEL_API_KEY") == "from-bundle"


def test_list_values_become_comma_separated(monkeypatch, tmp_path: Path):
    """JSON 里写列表比写逗号串自然，而环境变量那边本来就是逗号格式。"""
    monkeypatch.delenv("ALLOWED_ROOTS", raising=False)
    _write_config(tmp_path / "config.json", {"ALLOWED_ROOTS": ["D:/a", "D:/b"]})

    assert secrets.value("ALLOWED_ROOTS") == "D:/a,D:/b"


def test_describe_never_returns_values(monkeypatch, tmp_path: Path):
    """describe 的结果会进日志、进 HTTP 响应、进前端——它只能有来源。

    两个都要删：``load_settings`` 里的 ``load_dotenv`` 会把 .env **灌进
    os.environ**，所以跑过别的用例之后，宿主的 key 会以「环境变量」的身份
    出现在这里。
    """
    monkeypatch.setenv("MODEL_API_KEY", "sk-very-secret-value")
    monkeypatch.delenv("MAP_API_KEY", raising=False)

    described = secrets.describe(["MODEL_API_KEY", "MAP_API_KEY"])

    assert described == {"MODEL_API_KEY": secrets.ENV, "MAP_API_KEY": ""}
    assert "sk-very-secret-value" not in json.dumps(described)


def test_config_get_uses_the_chain(monkeypatch):
    """``config._get`` 必须真的走 secrets，而不是只在自己那层读环境变量。"""
    monkeypatch.delenv("SOME_UNSET_NAME", raising=False)
    monkeypatch.setattr(
        config.secrets, "value", lambda name: "from-chain" if name == "SOME_UNSET_NAME" else ""
    )

    assert config._get("SOME_UNSET_NAME") == "from-chain"


def test_env_still_wins_inside_config_get(monkeypatch):
    """回退链不能把「环境变量优先」搞丢。"""
    monkeypatch.setenv("SOME_SET_NAME", "from-env")
    monkeypatch.setattr(config.secrets, "value", lambda name: "from-chain")

    assert config._get("SOME_SET_NAME") == "from-env"


# ---------- 出包时的生成脚本 ----------


def test_generated_module_round_trips(tmp_path: Path):
    """生成物必须能被运行时原样读回来——盐和 payload 是两段分开的代码。"""
    env = tmp_path / ".env"
    env.write_text(
        "MODEL_API_KEY=sk-test-123\nMAP_API_KEY=amap-456\nREDIS_URL=redis://nope\n",
        encoding="utf-8",
    )
    out = tmp_path / "_bundled_keys.py"

    argv = ["--env-file", str(env), "--out", str(out)]
    import sys

    monkeypatch_argv = sys.argv
    sys.argv = ["make_bundled_keys", *argv]
    try:
        assert make_bundled_keys.main() == 0
    finally:
        sys.argv = monkeypatch_argv

    spec = importlib.util.spec_from_file_location("_generated_keys", out)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.keys() == {
        "MODEL_API_KEY": "sk-test-123",
        "MAP_API_KEY": "amap-456",
        # OVERRIDES 永远在，跟 .env 里写没写无关
        "STORAGE_BACKEND": "file",
        "SESSION_TTL_SEC": "0",
    }


def test_storage_backend_is_forced_to_file(tmp_path: Path):
    """开发机上是 redis，用户机器上没 Redis——出包时必须强制改成 file。

    这条是踩出来的：.env 里 ``STORAGE_BACKEND=redis``（当时在测多进程）
    被原样打进包里，用户的会话就存不进去了。
    """
    env = tmp_path / ".env"
    env.write_text("MODEL_API_KEY=sk-test\nSTORAGE_BACKEND=redis\n", encoding="utf-8")

    values = make_bundled_keys.parse_env_file(env)
    assert values["STORAGE_BACKEND"] == "redis"
    assert make_bundled_keys.OVERRIDES["STORAGE_BACKEND"] == "file"


def test_local_only_settings_are_not_bundled(tmp_path: Path):
    """REDIS_URL / WEB_AUTH_PASS 这类和「这台机器」绑定的东西不能进安装包。"""
    env = tmp_path / ".env"
    env.write_text(
        "MODEL_API_KEY=sk-test\n"
        "REDIS_URL=redis://localhost:6379/0\n"
        "WEB_AUTH_PASS=hunter2\n"
        "MEDIACRAWLER_PATH=D:/MediaCrawler\n",
        encoding="utf-8",
    )
    values = make_bundled_keys.parse_env_file(env)
    picked = {key for key in make_bundled_keys.INCLUDE if values.get(key)}

    assert picked == {"MODEL_API_KEY"}
    for local_only in ("REDIS_URL", "WEB_AUTH_PASS", "MEDIACRAWLER_PATH"):
        assert local_only not in make_bundled_keys.INCLUDE


def test_dry_run_does_not_print_secrets(tmp_path: Path, capsys):
    """--dry-run 的输出会被人贴进 issue。"""
    env = tmp_path / ".env"
    env.write_text("MODEL_API_KEY=sk-super-secret\n", encoding="utf-8")

    import sys

    monkeypatch_argv = sys.argv
    sys.argv = ["make_bundled_keys", "--env-file", str(env), "--dry-run"]
    try:
        assert make_bundled_keys.main() == 0
    finally:
        sys.argv = monkeypatch_argv

    printed = capsys.readouterr().out
    assert "MODEL_API_KEY" in printed
    assert "sk-super-secret" not in printed
