"""用户上传的附件：图片规范化后重编码，文档原样落盘。

**图片必须重新编码，不能原样存**，三个理由都是踩过才知道的：

1. 手机原图 12MP，base64 后约 4MB，一轮对话就把上下文预算吃穿——
   而且 token 是按像素算的，原图不会因为「模型看不了那么细」而便宜；
2. 手机竖拍照片靠 EXIF ``Orientation`` 标记方向，不摆正的话模型看到的是躺着的图，
   它会认真地描述一张歪掉的照片；
3. EXIF 里常有 GPS 坐标。用户传一张旅行照，不该顺手把拍摄位置带进模型上下文——
   这是隐私问题，不是性能问题。

**文档相反：一个字节都不改。** Word / Excel 是 zip 包，Pillow 不认识，重新编码
更是无从谈起；而且 ``read_document`` 那一套解析是按真实文件格式写的，改动过的
副本只会让解析失败。所以文档原样落盘，只按扩展名归档。

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
# 文档单独一档上限：带图的 docx、几万行的 xlsx 很容易过 12MB，
# 而文档不需要 base64 进上下文（只进摘要），体积压力小得多
MAX_DOC_BYTES = 32 * 1024 * 1024
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

# 能收的文档后缀。和 ``app/services/docs/reader.py`` 的 SUPPORTED_SUFFIXES 对齐，
# 另外**故意**收下 .doc / .xls：这两种旧格式读不了，但直接拒收只会让用户以为
# 软件坏了；收下来再回一句「另存为 .docx 再传」才是有用的反馈
DOC_SUFFIXES = frozenset(
    {".docx", ".xlsx", ".xlsm", ".doc", ".xls", ".txt", ".md", ".markdown", ".csv", ".tsv"}
)

# 取回文件时用的 Content-Type。浏览器拿到 octet-stream 会直接下载，
# 而 docx 若被当成文本渲染，用户看到的是乱码
DOC_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}

_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# 后缀只允许字母数字：文件名是用户给的，``../../x`` 这种不能进路径
_SAFE_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,8}$")

# 单条消息最多带几个附件。视频抽帧会一次占掉 4 个，留两个给「图 + 截图」的混发
MAX_ATTACHMENTS = 6

# 附件种类。image 会进多模态内容块，doc 走「摘要 + 路径」
KIND_IMAGE = "image"
KIND_DOC = "doc"


@dataclass(frozen=True)
class Upload:
    """一个已落盘的附件。元数据与文件分开放，改元数据不用重写文件。"""

    id: str
    ext: str
    width: int
    height: int
    bytes: int
    created_at: float
    user_id: str
    filename: str
    # image / doc。老记录没有这个字段，按 image 兜底（那时候只收图片）
    kind: str = KIND_IMAGE

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
            "kind": self.kind,
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
            kind=str(data.get("kind") or KIND_IMAGE),
        )

    @property
    def is_doc(self) -> bool:
        return self.kind == KIND_DOC


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

    def save_document(self, raw: bytes, user_id: str, filename: str = "") -> Upload:
        """原样保存一个文档，不做任何转码。

        **校验的是内容而不是扩展名**：docx / xlsx 都是 zip 包，头两个字节必然是
        ``PK``。只看扩展名的话，一个改了名字的可执行文件会被老老实实存下来，
        后面 ``read_document`` 解析时再炸——错误会出现在离原因很远的地方。
        """
        if len(raw) > MAX_DOC_BYTES:
            raise ValueError(f"文档超过上限 {MAX_DOC_BYTES // (1024 * 1024)}MB")
        suffix = doc_suffix_of(filename)
        if suffix in {".docx", ".xlsx", ".xlsm"} and not raw.startswith(b"PK"):
            raise ValueError(f"「{filename}」不是有效的 {suffix} 文件（内容与扩展名不符）")

        upload_id = uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{upload_id}{suffix}").write_bytes(raw)
        item = Upload(
            id=upload_id,
            ext=suffix,
            width=0,
            height=0,
            bytes=len(raw),
            created_at=time.time(),
            user_id=user_id,
            filename=filename[:120],
            kind=KIND_DOC,
        )
        self._write_meta(item)
        logger.info(
            "文档已接收 | id=%s 类型=%s 体积=%.0fKB 原文件名=%s",
            upload_id,
            suffix,
            len(raw) / 1024,
            filename or "-",
        )
        return item

    def _write_meta(self, item: Upload) -> None:
        (self.root / f"{item.id}.json").write_text(
            json.dumps(item.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
    # ---------- 读 ----------

    def get(self, upload_id: str, user_id: str | None = None) -> Upload | None:
        """按 id 取上传记录。

        ``user_id`` 不为 None 时校验归属，不属于这个人的按「不存在」返回。
        归属必须由**取用方**显式传进来：原图接口 ``GET /uploads/{id}`` 故意
        不校验（``<img src>`` 带不了自定义头），而「把它塞进某一轮对话」是
        写操作，必须校验——否则拿到 id 就能让别人的文件进入自己的上下文。
        """
        if not _UPLOAD_ID_RE.match(upload_id):
            return None
        meta = self.root / f"{upload_id}.json"
        if not meta.is_file():
            return None
        try:
            item = Upload.from_dict(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # 边车损坏就当没有这张图：宁可让用户重传，也不要抛 500
            logger.warning("上传元数据损坏 | id=%s", upload_id)
            return None
        if user_id is not None and item.user_id != user_id:
            return None
        return item

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


def doc_suffix_of(filename: str) -> str:
    """从文件名取文档后缀，取不到或不在白名单里就抛 ``ValueError``。

    白名单而不是黑名单：这个字符串会拼进落盘路径，任何「先取了再过滤」的写法
    都得考虑 ``../../x`` 和 ``a.b/c`` 这类构造。只认字母数字后缀，别的直接拒。
    """
    suffix = Path(filename or "").suffix.lower()
    if not _SAFE_SUFFIX_RE.match(suffix):
        raise ValueError(f"「{filename or '未命名文件'}」没有可用的扩展名")
    if suffix not in DOC_SUFFIXES:
        readable = "、".join(sorted(DOC_SUFFIXES))
        raise ValueError(f"不支持 {suffix} 文件，可以传：{readable}")
    return suffix


def mime_of(item: Upload) -> str:
    if item.kind == KIND_DOC:
        return DOC_MIME.get(item.ext, "application/octet-stream")
    return "image/png" if item.ext == ".png" else "image/jpeg"


def image_block(path: Path, mime: str) -> dict[str, Any]:
    """把本地图片包成 OpenAI 兼容的 ``image_url`` 内容块。

    用 data URL 而不是外链：模型服务商取不到我们 127.0.0.1 上的地址，
    要给它一个公网地址就得额外部署一次；base64 是唯一不需要额外条件就能用的方式。
    代价是体积膨胀约 33%，所以进上下文之前必须已经规范化到 1280（见 ``normalise_image``）。
    """
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}

