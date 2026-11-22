"""附件进模型上下文的那一段：图片走多模态块，文档走「摘要 + 路径」。

这段逻辑出错的方式很隐蔽——不会抛异常，只会让模型对着空内容自信地编。
所以这里断的是**内容**：摘要里有没有文档正文、路径是不是绝对路径、
旧格式有没有把「读不了」说清楚。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.core.config import load_settings
from app.core.security import register_root
from app.media.uploads import UploadStore
from app.services.chat import ChatService
from app.services.sessions import SessionService
from app.storage import MemorySessionStore
from app.storage.models import ChatMessage, ChatSession


def _docx_bytes() -> bytes:
    from docx import Document

    document = Document()
    document.add_heading("云南五日游服务合同", level=1)
    document.add_paragraph("甲方：{{客户名}}")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "金额"
    table.cell(1, 0).text = "机票"
    table.cell(1, 1).text = "3200"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.append(["客户", "城市", "金额"])
    for row in [("张三", "上海", 5200), ("李四", "三亚", 1800)]:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _png_bytes() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (40, 30), (30, 60, 90))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def service(tmp_path: Path) -> ChatService:
    return ChatService(
        load_settings(), SessionService(MemorySessionStore()), uploads=UploadStore(tmp_path)
    )


def test_docx_becomes_text_block_with_path(service: ChatService, tmp_path: Path):
    item = service._uploads.save_document(_docx_bytes(), "default", "云南合同.docx")

    blocks, images = service._context.attachment_blocks([item.id])

    assert images == []  # 文档不是图片，不能当参考图递下去
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    text = blocks[0]["text"]
    # 正文必须真的进来了，否则模型只能凭文件名猜
    assert "云南五日游服务合同" in text
    assert "3200" in text  # 表格里的数字也要读到
    # 路径必须是绝对的：工具按进程 CWD 解析相对路径，打包后 CWD 是 exe 目录
    assert str(service._uploads.path_of(item)) in text
    assert "query_sheet" in text and "aggregate_sheet" in text


def test_xlsx_summary_mentions_sheet_and_rows(service: ChatService):
    item = service._uploads.save_document(_xlsx_bytes(), "default", "订单.xlsx")

    blocks, _ = service._context.attachment_blocks([item.id])

    text = blocks[0]["text"]
    assert "订单.xlsx" in text
    assert "上海" in text
    # 摘要只给样本，精确统计要模型自己去调工具——这一点必须写在提示里，
    # 否则它会对着前几行心算一个总数出来
    assert "不要凭这份摘要自己算" in text


def test_legacy_doc_explains_why_it_cannot_be_read(service: ChatService):
    """2003 格式读不了，但要说清楚怎么办，而不是给一段空白。"""
    item = service._uploads.save_document(b"\xd0\xcf\x11\xe0legacy", "default", "旧合同.doc")

    blocks, _ = service._context.attachment_blocks([item.id])

    text = blocks[0]["text"]
    assert "读不出内容" in text
    assert "另存为" in text
    # 路径照样给：用户可能自己会用工具打开，模型也能据此解释文件在哪
    assert str(service._uploads.path_of(item)) in text


def test_table_file_is_marked_as_table(service: ChatService, tmp_path: Path):
    """表格要明说「这是表格，能直接传给 query_sheet / aggregate_sheet」。

    只给路径不够：模型会按扩展名猜，看到 .csv 以为汇总工具不支持，
    转头读全文自己加——实测就是这么错的（16600 是手算出来的）。
    """
    path = tmp_path / "订单.csv"
    path.write_text("客户,城市,金额\n张三,上海,5200\n", encoding="utf-8")
    item = service._uploads.save_document(path.read_bytes(), "default", "订单.csv")

    blocks, _ = service._context.attachment_blocks([item.id])

    assert "这是一份表格文件" in blocks[0]["text"]


def test_word_file_is_not_marked_as_table(service: ChatService):
    item = service._uploads.save_document(_docx_bytes(), "default", "合同.docx")

    blocks, _ = service._context.attachment_blocks([item.id])

    assert "这是一份表格文件" not in blocks[0]["text"]


def test_image_and_doc_can_be_mixed(service: ChatService):
    image = service._uploads.save_image(_png_bytes(), "default", "海边.png")
    doc = service._uploads.save_document(_xlsx_bytes(), "default", "订单.xlsx")

    blocks, images = service._context.attachment_blocks([image.id, doc.id])

    assert [block["type"] for block in blocks] == ["image_url", "text"]
    assert len(images) == 1  # 只有图片进参考图列表


def test_attachment_meta_distinguishes_kind(service: ChatService):
    image = service._uploads.save_image(_png_bytes(), "default", "海边.png")
    doc = service._uploads.save_document(_xlsx_bytes(), "default", "订单.xlsx")

    meta = service.attachment_meta([image.id, doc.id])

    assert meta[image.id]["kind"] == "image"
    assert meta[doc.id]["kind"] == "doc"
    assert meta[doc.id]["filename"] == "订单.xlsx"


def test_uploaded_doc_path_survives_into_next_turn(service: ChatService):
    """传过文件之后的下一轮，模型还得知道文件在哪。

    文档内容块只挂当轮（省 token），落库的只有 upload_id。如果重建历史时
    不把它变回一行路径，模型就只剩「按网点汇总」这句指令——它会去翻桌面、
    下载、文档三个目录，然后回一句「源文件已找不到」，而用户明明刚传过。
    2026-11-11 实测卡死在这里。
    """
    item = service._uploads.save_document(_xlsx_bytes(), "default", "7月下半旬.xlsx")
    message = ChatMessage(
        id="m1",
        role="user",
        content="按网点汇总一下",
        created_at="2026-11-11T14:00:00+00:00",
        attachments=[item.id],
    )

    text = service._context.history_text(message)

    assert text.startswith("按网点汇总一下")  # 用户原话不能被吃掉
    assert "7月下半旬.xlsx" in text
    assert str(service._uploads.path_of(item)) in text


def test_image_attachment_does_not_bloat_history(service: ChatService):
    """图片不往历史里补：一张 1280 的图约 1.5k token，而上一轮回答里已经有描述。"""
    image = service._uploads.save_image(_png_bytes(), "default", "海边.png")
    message = ChatMessage(
        id="m2",
        role="user",
        content="这张图好看吗",
        created_at="2026-11-11T14:00:00+00:00",
        attachments=[image.id],
    )

    assert service._context.history_text(message) == "这张图好看吗"


def test_deleted_upload_leaves_no_dangling_path(service: ChatService):
    """文件被清理掉之后别再补路径，否则模型照着调工具只会拿到「不存在」。"""
    item = service._uploads.save_document(_xlsx_bytes(), "default", "订单.xlsx")
    service._uploads.path_of(item).unlink()
    message = ChatMessage(
        id="m3",
        role="user",
        content="再汇总一次",
        created_at="2026-11-11T14:00:00+00:00",
        attachments=[item.id],
    )

    assert service._context.history_text(message) == "再汇总一次"


def test_only_doc_is_noted_when_image_and_doc_mixed(service: ChatService):
    image = service._uploads.save_image(_png_bytes(), "default", "海边.png")
    doc = service._uploads.save_document(_xlsx_bytes(), "default", "订单.xlsx")
    message = ChatMessage(
        id="m4",
        role="user",
        content="对照一下",
        created_at="2026-11-11T14:00:00+00:00",
        attachments=[image.id, doc.id],
    )

    text = service._context.history_text(message)

    assert "订单.xlsx" in text
    assert "海边.png" not in text


def test_assistant_message_is_left_alone(service: ChatService):
    message = ChatMessage(
        id="m5",
        role="assistant",
        content="好的",
        created_at="2026-11-11T14:00:00+00:00",
        attachments=[],
    )

    assert service._context.history_text(message) == "好的"


def test_uploads_root_is_readable_by_tools(tmp_path: Path):
    """上传目录要能被 read_document / query_sheet 读到。

    不注册的话，用户传完表格、模型要统计时会被「先授权这个目录」拦一下——
    而这个目录是程序自己建的，问用户毫无意义。
    """
    from app.agent.tools.system import _pending_grant, _roots
    from app.core.security import ensure_allowed

    register_root(tmp_path)
    target = tmp_path / "abc.xlsx"
    target.write_bytes(b"PK\x03\x04")

    # 两道闸都要过：一是不在允许范围内就拒（ensure_allowed），
    # 二是在范围内但没授权过要问（_pending_grant）
    assert ensure_allowed(target, _roots()) == target.resolve()
    assert _pending_grant(target) is None


def _session(*messages: ChatMessage, summarized: int = 0) -> ChatSession:
    session = ChatSession(id="s1", created_at="2026-11-12T09:00:00+00:00")
    session.messages = list(messages)
    session.summarized_count = summarized
    return session


def _user_with(attachments: list[str], text: str = "按网点做新表") -> ChatMessage:
    return ChatMessage(
        id="m-user",
        role="user",
        content=text,
        created_at="2026-11-12T09:00:00+00:00",
        attachments=attachments,
    )


def test_file_list_survives_summarization(service: ChatService):
    """摘要把带附件的那条消息折叠掉之后，路径仍然要在提示词里。

    2026-11-12 实测：用户第三次传同一个 Excel，模型还是回「我手上没有源
    文件名，请把 Excel 放到桌面」。原因是 summarized_count 推进后，那条消息
    不再进 messages，``_history_text`` 没机会补路径，而摘要器也没抄路径——
    它把 47k token 的对话压成一段「整理26个网点…」的散文，文件就丢了。
    """
    item = service._uploads.save_document(_xlsx_bytes(), "default", "7月下半旬.xlsx")
    session = _session(_user_with([item.id]), summarized=1)  # 已被摘要折叠

    block = service._context.session_files(session)

    assert "7月下半旬.xlsx" in block
    assert str(service._uploads.path_of(item)) in block
    # 光给路径不够：模型上一轮就是让用户把文件挪到桌面，这句要明说别这么做
    assert "不要让用户重新上传" in block


def test_file_list_dedupes_repeated_upload(service: ChatService):
    """同一个文件传两次只列一行——用户重传恰恰说明他以为没传上。"""
    first = service._uploads.save_document(_xlsx_bytes(), "default", "订单.xlsx")
    second = service._uploads.save_document(_xlsx_bytes(), "default", "订单.xlsx")
    session = _session(_user_with([first.id]), _user_with([second.id]), summarized=2)

    block = service._context.session_files(session)

    assert block.count("订单.xlsx") == 1


def test_file_list_skips_images(service: ChatService):
    """图片不进清单：上一轮回答里已经有描述，路径对模型没用。"""
    image = service._uploads.save_image(_png_bytes(), "default", "海边.png")
    session = _session(_user_with([image.id]), summarized=1)

    assert service._context.session_files(session) == ""


def test_file_list_skips_deleted_file(service: ChatService):
    """文件被清理掉就别再列，否则模型照着调工具只会拿到「不存在」。"""
    item = service._uploads.save_document(_xlsx_bytes(), "default", "订单.xlsx")
    service._uploads.path_of(item).unlink()
    session = _session(_user_with([item.id]), summarized=1)

    assert service._context.session_files(session) == ""


def test_file_list_is_capped(service: ChatService):
    """上限兜住：一个会话传十几份不该把提示词写成日志。"""
    from app.services.turn_context import MAX_SESSION_FILES

    messages = []
    for index in range(MAX_SESSION_FILES + 3):
        item = service._uploads.save_document(_xlsx_bytes(), "default", f"表{index}.xlsx")
        messages.append(_user_with([item.id]))
    session = _session(*messages, summarized=len(messages))

    block = service._context.session_files(session)

    listed = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(listed) == MAX_SESSION_FILES
