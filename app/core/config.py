"""配置读取：从项目根目录的 .env 加载环境变量。

注意：本环境未安装 pydantic-settings，故用 dotenv + dataclass 实现。
后续如需强校验，可平滑替换为 pydantic-settings。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# app/core/config.py -> app/core -> app -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


def _ensure_no_proxy() -> None:
    """把 localhost 加入 NO_PROXY。

    本地模型（LM Studio）与本地代理（Infinite Canvas）都在 127.0.0.1，
    若被系统代理拦截会导致请求超时或失败。
    """
    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = {item.strip() for item in current.split(",") if item.strip()}
    parts.update({"localhost", "127.0.0.1"})
    os.environ["NO_PROXY"] = ",".join(sorted(parts))


def _get(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _get_int(key: str, default: int) -> int:
    raw = _get(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_bool(key: str, default: bool) -> bool:
    raw = _get(key).lower()
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    return default


def _get_list(key: str) -> list[str]:
    raw = _get(key)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _derive_bearer(api_key: str) -> str:
    """图片走的是与火山方舟同一套鉴权，未显式填写 token 时自动复用。"""
    return f"Bearer {api_key}" if api_key else ""


@dataclass(frozen=True)
class Settings:
    # 大模型
    model_provider: str
    model_name: str
    model_api_key: str
    model_base_url: str
    max_context_tokens: int
    model_tool_calling: str

    # 图片
    image_backend: str
    custom_image_endpoint: str
    custom_image_token: str
    custom_image_model: str
    custom_image_size: str
    custom_image_response_format: str
    custom_image_watermark: bool

    # 视频
    video_backend: str
    ark_api_key: str
    ark_base_url: str
    ark_video_model: str
    ark_video_ratio: str
    ark_video_generate_audio: bool
    ark_video_watermark: bool
    ark_video_poll_initial_sec: float
    ark_video_poll_max_sec: float
    ark_video_timeout_sec: float

    # 媒体任务
    media_task_ttl_sec: int

    # 存储
    storage_backend: str
    redis_url: str
    session_ttl_sec: int

    # 检索类 Key
    map_api_key: str
    weather_api_key: str
    bing_search_api_key: str

    # 检索层（SPEC 4.7 / 4.8）
    source_timeout_sec: int
    enable_mock_fallback: bool
    fetch_timeout_sec: int
    max_fetch_bytes: int

    # 酒店价格（LiteAPI）
    liteapi_key: str
    liteapi_base_url: str

    # 沙箱与服务
    allowed_roots: list[str]
    cors_origins: list[str]

    # 登录态采集（plugin 组 B，默认关闭，个人自用，不随核心开源发布）
    enable_session_fetch: bool
    cookie_dir: str
    mediacrawler_path: str
    mediacrawler_timeout: int
    crawler_platforms: list[str]


def load_settings() -> Settings:
    """加载 .env 并返回配置对象。"""
    load_dotenv(ENV_FILE, encoding="utf-8-sig", override=False)
    _ensure_no_proxy()

    return Settings(
        model_provider=_get("MODEL_PROVIDER", "openai_compatible"),
        model_name=_get("MODEL_NAME"),
        model_api_key=_get("MODEL_API_KEY"),
        model_base_url=_get("MODEL_BASE_URL"),
        max_context_tokens=_get_int("MAX_CONTEXT_TOKENS", 4096),
        model_tool_calling=_get("MODEL_TOOL_CALLING", "auto").lower(),
        image_backend=_get("IMAGE_BACKEND", "none").lower(),
        custom_image_endpoint=_get("CUSTOM_IMAGE_ENDPOINT"),
        custom_image_token=_get("CUSTOM_IMAGE_TOKEN") or _derive_bearer(_get("ARK_API_KEY")),
        custom_image_model=_get("CUSTOM_IMAGE_MODEL"),
        custom_image_size=_get("CUSTOM_IMAGE_SIZE", "2K"),
        custom_image_response_format=_get("CUSTOM_IMAGE_RESPONSE_FORMAT", "url"),
        custom_image_watermark=_get_bool("CUSTOM_IMAGE_WATERMARK", False),
        video_backend=_get("VIDEO_BACKEND", "none").lower(),
        ark_api_key=_get("ARK_API_KEY"),
        ark_base_url=_get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
        ark_video_model=_get("ARK_VIDEO_MODEL"),
        ark_video_ratio=_get("ARK_VIDEO_RATIO", "adaptive"),
        ark_video_generate_audio=_get_bool("ARK_VIDEO_GENERATE_AUDIO", True),
        ark_video_watermark=_get_bool("ARK_VIDEO_WATERMARK", False),
        ark_video_poll_initial_sec=float(_get_int("ARK_VIDEO_POLL_INITIAL_SEC", 3)),
        ark_video_poll_max_sec=float(_get_int("ARK_VIDEO_POLL_MAX_SEC", 10)),
        ark_video_timeout_sec=float(_get_int("ARK_VIDEO_TIMEOUT_SEC", 600)),
        media_task_ttl_sec=_get_int("MEDIA_TASK_TTL_SEC", 3600),
        storage_backend=_get("STORAGE_BACKEND", "memory").lower(),
        redis_url=_get("REDIS_URL", "redis://localhost:6379/0"),
        session_ttl_sec=_get_int("SESSION_TTL_SEC", 0),
        map_api_key=_get("MAP_API_KEY"),
        weather_api_key=_get("WEATHER_API_KEY"),
        bing_search_api_key=_get("BING_SEARCH_API_KEY"),
        source_timeout_sec=_get_int("SOURCE_TIMEOUT_SEC", 20),
        enable_mock_fallback=_get_bool("ENABLE_MOCK_FALLBACK", True),
        fetch_timeout_sec=_get_int("FETCH_TIMEOUT_SEC", 15),
        max_fetch_bytes=_get_int("MAX_FETCH_BYTES", 1_500_000),
        liteapi_key=_get("LITEAPI_KEY"),
        liteapi_base_url=_get("LITEAPI_BASE_URL", "https://api.liteapi.travel/v3.0"),
        allowed_roots=_get_list("ALLOWED_ROOTS"),
        cors_origins=_get_list("CORS_ORIGINS") or ["http://localhost:3001"],
        enable_session_fetch=_get_bool("ENABLE_SESSION_FETCH", False),
        cookie_dir=_get("COOKIE_DIR", "cookies"),
        mediacrawler_path=_get("MEDIACRAWLER_PATH", "D:/MediaCrawler"),
        mediacrawler_timeout=_get_int("MEDIACRAWLER_TIMEOUT", 200),
        crawler_platforms=_get_list("CRAWLER_PLATFORMS")
        or ["douyin", "xiaohongshu", "bilibili", "weibo", "zhihu", "tieba"],
    )
