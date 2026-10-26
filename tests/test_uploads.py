"""用户上传图片的规范化与落盘。

这里的断言都不是「跑通就行」，每一条对应一个具体的坑：

- 12MP 原图不缩，一轮对话的 token 就被一张图吃穿；
- 手机竖拍靠 EXIF 标记方向，不摆正模型看到的是躺着的图；
- EXIF 里带 GPS，原样进上下文等于把拍摄地点送出去；
- 坏文件要让调用方拿到 ``ValueError``（→ 415），而不是让 Pillow 的异常冒成 500。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.media.uploads import (
    MAX_SIDE,
    MAX_UPLOAD_BYTES,
    UploadStore,
    normalise_image,
)


def png_bytes(width: int, height: int, color=(200, 40, 40), alpha: bool = False) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    image = Image.new(mode, (width, height), color + ((255,) if alpha else ()))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def jpeg_with_orientation(width: int, height: int, orientation: int) -> bytes:
    """造一张带 EXIF 方向的图：orientation=6 表示「需要顺时针转 90 度才正」。"""
    image = Image.new("RGB", (width, height), (10, 20, 30))
    exif = image.getexif()
    exif[274] = orientation  # 274 = Orientation
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


# ---------- 规范化 ----------


def test_long_side_is_capped():
    data, ext, width, height = normalise_image(png_bytes(3000, 2000))
    assert max(width, height) == MAX_SIDE
    # 长宽比必须保持，否则模型看到的构图是变形的
    assert round(width / height, 2) == 1.5
    assert ext in {".jpg", ".png"}
    assert data


def test_small_image_is_not_upscaled():
    """小图放大只会变糊，还多花 token。"""
    _, _, width, height = normalise_image(png_bytes(320, 240))
    assert (width, height) == (320, 240)


def test_opaque_png_becomes_jpeg():
    """没有透明通道的 PNG 转 JPEG：同样画质下体积小一个数量级。"""
    _, ext, _, _ = normalise_image(png_bytes(400, 300))
    assert ext == ".jpg"


def test_transparent_png_keeps_png():
    """有透明区就保留 PNG——截图和贴纸压成 JPEG 会出现黑边和毛边。"""
    data, ext, _, _ = normalise_image(png_bytes(200, 200, alpha=True))
    assert ext == ".png"
    assert data.startswith(b"\x89PNG")


def test_exif_orientation_is_applied():
    """orientation=6 的竖拍图，摆正后宽高应当互换。"""
    _, _, width, height = normalise_image(jpeg_with_orientation(400, 200, 6))
    assert (width, height) == (200, 400)


def test_exif_gps_is_stripped():
    """重新编码会丢掉 EXIF：用户传一张旅行照，不该把拍摄坐标带进模型上下文。"""
    image = Image.new("RGB", (200, 150), (1, 2, 3))
    exif = image.getexif()
    exif[274] = 1
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    data, _, _, _ = normalise_image(buffer.getvalue())

    with Image.open(io.BytesIO(data)) as saved:
        assert not dict(saved.getexif())


def test_garbage_is_rejected_as_value_error():
    """坏文件必须抛 ValueError（路由据此回 415），不能是 Pillow 的原生异常。"""
    with pytest.raises(ValueError):
        normalise_image("这不是图片".encode())


def test_empty_bytes_is_rejected():
    with pytest.raises(ValueError):
        normalise_image(b"")


# ---------- 落盘 ----------


def test_save_writes_file_and_metadata(tmp_path):
    store = UploadStore(tmp_path)
    item = store.save_image(png_bytes(500, 400), "device-1", "海边.png")

    assert (tmp_path / f"{item.id}.json").is_file()
    assert store.path_of(item).is_file()
    assert (item.width, item.height) == (500, 400)
    assert item.filename == "海边.png"

    again = store.get(item.id)
    assert again is not None
    assert again.to_dict() == item.to_dict()


def test_oversized_upload_is_rejected(tmp_path):
    store = UploadStore(tmp_path)
    with pytest.raises(ValueError):
        store.save_image(b"\x00" * (MAX_UPLOAD_BYTES + 1), "device-1")


def test_bad_id_never_touches_disk(tmp_path):
    """upload_id 直接来自 URL，必须挡住 ``../`` 这类东西。"""
    store = UploadStore(tmp_path)
    assert store.get("../../etc/passwd") is None
    assert store.get("..") is None
    assert store.get("") is None


def test_delete_is_scoped_to_owner(tmp_path):
    store = UploadStore(tmp_path)
    item = store.save_image(png_bytes(200, 200), "device-1")

    assert store.delete(item.id, "someone-else") is False
    assert store.path_of(item).is_file()

    assert store.delete(item.id, "device-1") is True
    assert store.get(item.id) is None
    assert not store.path_of(item).exists()


def test_corrupt_metadata_is_treated_as_missing(tmp_path):
    """边车损坏时宁可让用户重传，也不要抛 500 把整轮对话带崩。"""
    store = UploadStore(tmp_path)
    item = store.save_image(png_bytes(100, 100), "device-1")
    (tmp_path / f"{item.id}.json").write_text("{ 不是 json", encoding="utf-8")

    assert store.get(item.id) is None


# ---------- 进上下文的那一步 ----------


def test_image_block_is_a_base64_data_url(tmp_path):
    """模型服务商取不到 127.0.0.1，data URL 是唯一不需要额外部署的方式。"""
    from app.media.uploads import image_block, mime_of

    store = UploadStore(tmp_path)
    item = store.save_image(png_bytes(120, 120, alpha=True), "device-1")
    block = image_block(store.path_of(item), mime_of(item))

    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


def test_missing_attachment_is_skipped_not_fatal(tmp_path):
    """一张图被删掉不该让整轮对话失败：其余几张还能用。

    直接测私有方法是有意的——这一段是「图片进上下文」的全部逻辑，
    走 HTTP 只能看到最终回答，看不到它到底传了几张、传的是哪张。
    """
    from app.core.config import load_settings
    from app.services.chat import ChatService
    from app.storage import MemorySessionStore

    store = UploadStore(tmp_path)
    item = store.save_image(png_bytes(100, 100, alpha=True), "device-1")
    service = ChatService(load_settings(), MemorySessionStore(), uploads=store)

    blocks = service._image_blocks([item.id, "0" * 32])
    assert len(blocks) == 1
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_no_uploads_configured_yields_no_blocks():
    from app.core.config import load_settings
    from app.services.chat import ChatService
    from app.storage import MemorySessionStore

    service = ChatService(load_settings(), MemorySessionStore())
    assert service._image_blocks(["0" * 32]) == []
