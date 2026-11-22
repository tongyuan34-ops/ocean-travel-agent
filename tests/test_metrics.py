"""指标注册表与 /metrics 端点的测试。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import metrics as metrics_routes
from app.core.metrics import Metrics, MetricsMiddleware, metrics


@pytest.fixture(autouse=True)
def _clean_metrics() -> None:
    metrics.reset()


def test_counter_groups_by_labels() -> None:
    registry = Metrics()
    registry.inc("http_requests_total", method="GET", path="/health", status="200")
    registry.inc("http_requests_total", 2, method="GET", path="/health", status="200")
    registry.inc("http_requests_total", method="POST", path="/health", status="500")

    series = registry.snapshot()["series"]["http_requests_total"]
    values = {
        (item["labels"]["method"], item["labels"]["status"]): item["value"] for item in series
    }
    assert values[("GET", "200")] == 3
    assert values[("POST", "500")] == 1


def test_histogram_percentiles_and_prometheus_text() -> None:
    registry = Metrics()
    for value in (0.001, 0.002, 0.003, 0.004):
        registry.observe("http_request_duration_seconds", value, path="/slow")

    row = registry.snapshot()["series"]["http_request_duration_seconds"][0]
    assert row["count"] == 4
    assert row["p50_ms"] is not None and row["p50_ms"] > 0
    assert row["p95_ms"] >= row["p50_ms"]

    text = registry.prometheus()
    assert 'http_request_duration_seconds_count{path="/slow"} 4' in text
    assert 'http_request_duration_seconds_bucket{path="/slow",le="+Inf"} 4' in text


def test_middleware_counts_requests_and_skips_metrics_path() -> None:
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
        assert client.get("/metrics").status_code == 404

    series = metrics.snapshot()["series"]["http_requests_total"]
    labels = [item["labels"] for item in series]
    # 路由模板是 /ping，不是请求路径；/metrics 被跳过，不会出现
    assert any(item["path"] == "/ping" and item["status"] == "200" for item in labels)
    assert all(item["path"] != "/metrics" for item in labels)


def test_middleware_records_unknown_path_as_unmatched() -> None:
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    with TestClient(app) as client:
        assert client.get("/no-such-path/123").status_code == 404

    labels = [item["labels"] for item in metrics.snapshot()["series"]["http_requests_total"]]
    assert any(item["path"] == "unmatched" and item["status"] == "404" for item in labels)


def test_metrics_endpoints_expose_json_and_text() -> None:
    app = FastAPI()
    app.include_router(metrics_routes.router, prefix="/api/v1")
    app.include_router(metrics_routes.text_router)
    metrics.inc("chat_turns_total", mode="native", skill="none")

    with TestClient(app) as client:
        data = client.get("/api/v1/metrics").json()
        assert data["uptime_seconds"] >= 0
        assert data["series"]["chat_turns_total"][0]["value"] == 1

        text = client.get("/metrics")
        assert text.status_code == 200
        assert text.text.startswith('chat_turns_total{mode="native",skill="none"} 1')
