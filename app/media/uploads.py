"""用户上传的图片：规范化、落盘、按设备归属。

**必须重新编码，不能原样存**，三个理由都是踩过才知道的：

1. 手机原图 12MP，base64 后约 4MB，一轮对话就把上下文预算吃穿——
   而且 token 是按像素算的，原图不会因为「模型看不了那么细」而便宜；
2. 手机竖拍照片靠 EXIF ``Orientation`` 标记方向，不摆正的话模型看到的是躺着的图，
   它会认真地描述一张歪掉的照片；
3. EXIF 里常有 GPS 坐标。用户传一张旅行照，不该顺手把拍摄位置带进模型上下文——
   这是隐私问题，不是性能问题。

**接口不走 multipart**：一次只传一个文件，直接读请求体就够，
省掉 ``python-multipart`` 一个依赖和一个解析器；浏览器侧 ``xhr.send(file)``
本来就是这么发的。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# 单文件上限。12MB 足够覆盖手机原图，又不至于让内存里出现离谱的请求体
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
# 长边上限：1280 对「看图回答问题」够用，再大只是烧 token
MAX_SIDE = 1280
JPEG_QUALITY = 85
# PNG 保留无损（截图里的文字经 JPEG 会有毛边），但超过这个体积就压成 JPEG
PNG_KEEP_LIMIT = 2 * 1024 * 1024

ALLOWED_MIME = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
}

_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# 单条消息最多带几张图。视频抽帧会一次占掉 4 张，留两张给「图 + 截图」的混发
MAX_ATTACHMENTS = 6


@dataclass(frozen=True)
class Upload:
    """一张已落盘的图片。元数据与文件分开放，改元数据不用重写图片。"""

    id: str
    ext: str
    width: int
    height: int
    bytes: int
    created_at: float
    user_id: str
    filename: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "ext": self.ext,
            "width": self.width,
            "height": self.height,
            "bytes": self.bytes,
            "created_at": self.created_at,
            "user_id": self.user_id,
            "filename": self.filename,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Upload:
        return cls(
            id=str(data.get("id") or ""),
            ext=str(data.get("ext") or ".jpg"),
            width=_as_int(data.get("width")),
            height=_as_int(data.get("height")),
            bytes=_as_int(data.get("bytes")),
            created_at=_as_float(data.get("created_at")),
            user_id=str(data.get("user_id") or "default"),
            filename=str(data.get("filename") or ""),
        )


def _as_int(value: object, default: int = 0) -> int:
    """边车 JSON 里的数字可能是字符串（手改过、或者旧版本写坏了），不能直接 int()。"""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def normalise_image(raw: bytes) -> tuple[bytes, str, int, int]:
    """摆正、缩放、重新编码，返回 ``(字节, 扩展名, 宽, 高)``。

    坏文件抛 ``ValueError``——调用方据此回 415，而不是让 Pillow 的
    ``UnidentifiedImageError`` 冒到全局异常处理器变成一个 500。
    """
    try:
        with Image.open(io.BytesIO(raw)) as src:
            # 先按 EXIF 摆正再缩放：反过来的话，宽高判断会按躺着的尺寸算
            im: Image.Image = ImageOps.exif_transpose(src)
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )
            # 动图只取第一帧：多帧内容无法进入单条消息，装作能理解反而误导用户
            im = im.convert("RGBA" if has_alpha else "RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)

            if has_alpha:
                buf = io.BytesIO()
                im.save(buf, "PNG", optimize=True)
                if buf.tell() <= PNG_KEEP_LIMIT:
                    return buf.getvalue(), ".png", im.width, im.height
                # 透明图压不进 2MB：宁可丢 alpha 也要控制体积，照片类通常没透明区
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
                return buf.getvalue(), ".jpg", im.width, im.height

            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
            return buf.getvalue(), ".jpg", im.width, im.height
    except Image.DecompressionBombError as exc:  # 像素数离谱的构造图
        raise ValueError(f"图片像素数超出上限：{exc}") from None
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法解析为图片：{exc}") from None


class UploadStore:
    """把上传的图片放在一个目录里，元数据写成同名 ``.json`` 边车。

    不用 Redis：上传是一次性的短期数据，跟着进程走没有意义——
    重启后历史消息里的图还得能打开，落盘反而更合适。
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    # ---------- 写 ----------

    def save_image(self, raw: bytes, user_id: str, filename: str = "") -> Upload:
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError(f"文件超过上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
        data, ext, width, height = normalise_image(raw)

        upload_id = uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{upload_id}{ext}").write_bytes(data)
        item = Upload(
            id=upload_id,
            ext=ext,
            width=width,
            height=height,
            bytes=len(data),
            created_at=time.time(),
            user_id=user_id,
            filename=filename[:120],
        )
        self._write_meta(item)
        logger.info(
            "图片已接收 | id=%s 尺寸=%dx%d 体积=%.0fKB 原文件名=%s",
            upload_id,
            width,
            height,
            len(data) / 1024,
            filename or "-",
        )
        return item

    def _write_meta(self, item: Upload) -> None:
        (self.root / f"{item.id}.json").write_text(
            json.dumps(item.to_dict(), ensure_ascii=False), encoding="utf-8"
        )

    # ---------- 读 ----------

    def get(self, upload_id: str) -> Upload | None:
        if not _UPLOAD_ID_RE.match(upload_id):
            return None
        meta = self.root / f"{upload_id}.json"
        if not meta.is_file():
            return None
        try:
            return Upload.from_dict(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # 边车损坏就当没有这张图：宁可让用户重传，也不要抛 500
            logger.warning("上传元数据损坏 | id=%s", upload_id)
            return None

    def path_of(self, item: Upload) -> Path:
        return self.root / f"{item.id}{item.ext}"

    # ---------- 删 ----------

    def delete(self, upload_id: str, user_id: str) -> bool:
        item = self.get(upload_id)
        if item is None or item.user_id != user_id:
            return False
        for path in (self.path_of(item), self.root / f"{item.id}.json"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("删除上传文件失败 | path=%s", path)
        return True


def mime_of(item: Upload) -> str:
    return "image/png" if item.ext == ".png" else "image/jpeg"


def image_block(path: Path, mime: str) -> dict[str, Any]:
    """把本地图片包成 OpenAI 兼容的 ``image_url`` 内容块。

    用 data URL 而不是外链：模型服务商取不到我们 127.0.0.1 上的地址，
    要给它一个公网地址就得额外部署一次；base64 是唯一不需要额外条件就能用的方式。
    代价是体积膨胀约 33%，所以进上下文之前必须已经规范化到 1280（见 ``normalise_image``）。
    """
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}

