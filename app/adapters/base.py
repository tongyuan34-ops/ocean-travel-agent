"""数据源适配器基类（SPEC 4.7）。

所有数据源统一继承 SourceAdapter，由 registry 注册。
统一入口 `search()` 承担三件事，子类只管实现 `_search()`：

1. **可用性判断**：available()=False 时直接跳过或降级，不发请求
2. **超时**：外层超时按 `search_timeout` 或 `max(source_timeout*3, 45)` 施加
   （SPEC 4.8 铁律：外层超时必须 ≥ 内层超时，否则子进程/上游未跑完就被提前杀死）
3. **降级**：源内抛异常时按 ENABLE_MOCK_FALLBACK 决定返回带 mock 标记的占位结果还是空列表

**铁律：任何单源失败都不得拖垮整体流程**，因此异常绝不允许从这里冒出去。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time

from app.core.config import Settings
from app.core.models import Doc, SourceKind, source_kind_value

logger = logging.getLogger(__name__)

# 通用源的超时下限（SPEC 4.8）
MIN_SOURCE_TIMEOUT = 45.0


class SourceAdapter(abc.ABC):
    """检索源适配器。

    子类以类属性声明元信息，构造时注入 Settings。
    """

    name: str = ""
    kind: SourceKind = SourceKind.GENERAL_CN
    platform: str = ""
    # 是否可被模型当作「检索源」自行选择。
    # fetch_url 这类「按 URL 抓正文」必须为 False，否则会被当成搜索引擎调度。
    selectable_by_llm: bool = True
    # 显式超时；0 表示用默认 max(source_timeout*3, 45)
    search_timeout: float = 0.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @abc.abstractmethod
    def available(self) -> bool:
        """当前配置下该源是否可用。"""

    def unavailable_reason(self) -> str:
        """不可用的原因，用于 capabilities 与日志。"""
        return "未启用"

    @abc.abstractmethod
    async def _search(self, query: str, limit: int) -> list[Doc]:
        """真正执行检索。失败请直接抛异常，由 search() 统一降级。"""

    def timeout(self) -> float:
        if self.search_timeout > 0:
            return self.search_timeout
        return max(float(self._settings.source_timeout_sec) * 3, MIN_SOURCE_TIMEOUT)

    async def search(self, query: str, limit: int) -> list[Doc]:
        """统一检索入口：可用性 + 超时 + 单源失败降级。"""
        started = time.perf_counter()

        if not self.available():
            reason = self.unavailable_reason()
            results = self._degrade(query, reason)
            self._log(started, len(results), degraded=True, detail=reason)
            return results

        try:
            docs = await asyncio.wait_for(
                self._search(query, limit), timeout=self.timeout()
            )
        except asyncio.TimeoutError:
            detail = f"超时（>{self.timeout():.0f}s）"
            results = self._degrade(query, detail)
            self._log(started, len(results), degraded=True, detail=detail)
            return results
        except Exception as exc:  # noqa: BLE001 - 单源失败必须被隔离
            detail = f"{type(exc).__name__}: {exc}"
            results = self._degrade(query, detail)
            self._log(started, len(results), degraded=True, detail=detail)
            return results

        self._log(started, len(docs), degraded=False)
        return docs

    # ---------- 内部 ----------

    def _degrade(self, query: str, reason: str) -> list[Doc]:
        """降级结果：ENABLE_MOCK_FALLBACK=true 时返回占位 Doc，否则空列表。"""
        if not self._settings.enable_mock_fallback:
            return []
        return [
            Doc(
                title=f"【降级占位】{self.platform}本次未返回真实结果",
                url="",
                snippet=reason,
                source=self.name,
                source_kind=source_kind_value(self.kind),
                platform=self.platform,
                mock=True,
                extras={"reason": reason, "query": query},
            )
        ]

    def _log(self, started: float, hits: int, *, degraded: bool, detail: str = "") -> None:
        elapsed = time.perf_counter() - started
        logger.info(
            "检索源 %s | available=%s | 命中 %d | 耗时 %.2fs | %s%s",
            self.name,
            self.available(),
            hits,
            elapsed,
            "降级/mock" if degraded else "正常",
            f" | {detail}" if detail else "",
        )
