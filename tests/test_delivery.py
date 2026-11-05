"""行程交付：PDF / 路线图两个工具，以及取文件的接口。

这里只测「不依赖高德」的部分：地图在测试里一律打桩成 None，
因为真正的底图要靠外部服务，测试不该联网。PDF 本身是本地渲染，
所以能真出文件、真读回字节——只断言「返回了一个字符串」等于没测。
"""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from app.agent.tools.delivery import export_trip_pdf, render_trip_map
from app.services import trip_export

TRIP_ARGS = {
    "title": "三亚 3 天亲子",
    "city": "三亚",
    "origin": "三亚凤凰机场",
    "start_date": "2026-11-06",
    "days": [
        {"title": "落地 + 亚龙湾", "stops": ["亚龙湾", "亚龙湾热带天堂森林公园"]},
        {"title": "蜈支洲岛", "stops": ["蜈支洲岛", "后海村"]},
        {"title": "返程", "stops": ["三亚国际免税城"]},
    ],
}


@pytest.fixture
def no_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    """地图一律「拿不到」——PDF 仍然必须出得来。"""

    async def fake(plan):
        return None, [None for _ in plan.days]

    monkeypatch.setattr(trip_export, "maps_for_plan", fake)


def test_export_trip_pdf_renders_a_real_file(
    monkeypatch: pytest.MonkeyPatch, no_maps: None, tmp_path
) -> None:
    """地图全失败也要出文档，且返回的链接指向真实文件。"""
    monkeypatch.setattr(trip_export, "PDF_DIR", tmp_path)

    out = asyncio.run(export_trip_pdf.ainvoke(TRIP_ARGS))

    match = re.search(r"/trips/files/(trip-[0-9a-f]{16}\.pdf)", out)
    assert match, out
    assert out.startswith("PDF 已生成")
    data = (tmp_path / match.group(1)).read_bytes()
    assert data.startswith(b"%PDF")
    # 3 天 5 个地点要体现在给模型的摘要里，否则它没法跟用户复述
    assert "3 天" in out and "5 个地点" in out


def test_export_trip_pdf_rejects_empty_plan() -> None:
    out = asyncio.run(export_trip_pdf.ainvoke({"days": [{"stops": []}]}))
    assert out.startswith("INVALID_REQUEST")


def test_export_trip_pdf_reuses_the_same_file(
    monkeypatch: pytest.MonkeyPatch, no_maps: None, tmp_path
) -> None:
    """同一份行程连生成两次只渲染一次——按钮和对话两条路径共用一个缓存键。"""
    monkeypatch.setattr(trip_export, "PDF_DIR", tmp_path)

    first = asyncio.run(export_trip_pdf.ainvoke(TRIP_ARGS))
    second = asyncio.run(export_trip_pdf.ainvoke(TRIP_ARGS))

    assert first.splitlines()[0] == second.splitlines()[0]
    assert len(list(tmp_path.glob("trip-*.pdf"))) == 1


def test_render_trip_map_reports_missing_key_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没配高德 Key 时返回错误码字符串，不能抛异常把整轮对话打断。"""
    from app.services import trips as trips_service
    from app.services.trips import TripMapError

    async def boom(*args, **kwargs):
        raise TripMapError("TOOL_NOT_ENABLED", "未配置 MAP_API_KEY，无法生成地图")

    # 工具里是函数内导入，打桩要打在服务模块上
    monkeypatch.setattr(trips_service, "build_map", boom)
    out = asyncio.run(render_trip_map.ainvoke({"stops": ["亚龙湾", "后海村"]}))
    assert out.startswith("TOOL_NOT_ENABLED")


def test_render_trip_map_requires_stops() -> None:
    out = asyncio.run(render_trip_map.ainvoke({"stops": ["  "]}))
    assert out.startswith("INVALID_REQUEST")


def test_files_endpoint_rejects_names_outside_the_whitelist() -> None:
    """只认 trip-/map- 加 16 位 hex。拼路径、改后缀都要挡在门外。"""
    from app.main import app

    with TestClient(app) as client:
        for bad in (
            "app.log",
            "trip-xyz.pdf",
            "trip-0123456789abcdef.exe",
            "..%2F..%2Fapp.log",
            "trip-0123456789abcdef.pdf.bak",
        ):
            resp = client.get(f"/api/v1/trips/files/{bad}")
            assert resp.status_code in (400, 404), bad


def test_files_endpoint_404s_when_the_file_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """文件名合法但文件已被清理时，要回 404 而不是 500。

    这条是实测发现的：``ApiError("NOT_FOUND", ...)`` 里的码原先不在
    ``CODE_TO_HTTP`` 里，于是被当成未知错误落到 500——用户点一个过期链接
    看到的是「服务器内部错误」，还会被日志记成未处理异常。
    """
    from app.api.routes import trips as trips_route
    from app.main import app

    monkeypatch.setattr(trips_route, "PDF_DIR", tmp_path)

    with TestClient(app) as client:
        resp = client.get("/api/v1/trips/files/trip-0123456789abcdef.pdf")
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"


def test_files_endpoint_serves_a_generated_pdf(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from app.api.routes import trips as trips_route
    from app.main import app

    monkeypatch.setattr(trips_route, "PDF_DIR", tmp_path)
    name = "trip-0123456789abcdef.pdf"
    (tmp_path / name).write_bytes(b"%PDF-1.4 fake")

    with TestClient(app) as client:
        resp = client.get(f"/api/v1/trips/files/{name}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")


def test_geocode_cache_avoids_duplicate_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一次导出里总览图和每日图会重复解析同一批地点，只该打一次高德。"""
    from app.agent.tools import info

    calls: list[str] = []

    async def fake(address: str, city: str | None = None):
        calls.append(address)
        return ("120.0,30.0", "杭州")

    monkeypatch.setattr(info, "_geocode_uncached", fake)
    first = asyncio.run(info._geocode("西湖", "杭州"))
    second = asyncio.run(info._geocode("西湖", "杭州"))

    assert first == second
    assert calls == ["西湖"]
