"""一轮对话的上下文装配：附件、历史、事实、摘要与提示词。

这些逻辑的共同点：**都在回答「这一轮该让模型看到什么」**，且都不涉及流式输出。
拆出来的直接好处是它们可以脱离 ChatService 单测——附件解析、摘要折叠、
文件清单这些分支出过错（见各方法里的实测记录），值得能被单独钉住。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from app.agent.model import build_chat_model
from app.agent.prompts import build_system_prompt
from app.core.config import Settings
from app.core.context import compose_messages, make_summary, plan_fold
from app.media.service import MediaService
from app.media.uploads import MAX_ATTACHMENTS, Upload, UploadStore, image_block, mime_of
from app.services.docs.reader import DocError
from app.services.docs.reader import read_document as read_doc_text
from app.services.docs.sheet import SHEET_SUFFIXES
from app.services.facts import (
    MediaFact,
    build_turn_facts,
    last_warnings,
    recent_task_ids,
)
from app.services.profile import ProfileService
from app.skills import catalog_block, instructions_block
from app.storage import ChatMessage, ChatSession

logger = logging.getLogger(__name__)

# 每个会话保留最近多少轮对话（1 轮 = 一问一答）
MAX_TURNS = 20
# 单轮最多几张图能当生图参考图。实测上游接受两张的数组，
# 再多只是把请求体撑大（一张 1280 的图 base64 后约 250KB）
MAX_REF_IMAGES = 2
# 上传的文档最多把多少块摘进上下文。40 块约等于「一份合同的前三节 + 两张表」，
# 足够回答「这是什么、大概讲了什么」；再深的问题让模型调 read_document 续读，
# 一次摊开两百块只是把上下文预算花在了没人问的段落上
DOC_BLOCKS_IN_PROMPT = 40
# 会话文件清单最多列这么多个。一行一个路径、几十个 token，但一个会话里
# 反复传十几份的情况真会发生，全列出来就把提示词写成日志了
MAX_SESSION_FILES = 8


@dataclass(frozen=True)
class TurnPrompt:
    """这一轮交给模型的东西，一次性装好。"""

    system_text: str
    messages: list[Any]
    # 上传图片的 data URL，给生图工具当参考图
    refs: list[str]
    # 带文件路径的历史正文，用量估算与偏好抽取都会用到
    history: list[tuple[str, str]]


class TurnContext:
    """把「用户说了什么 + 传了什么 + 之前聊了什么」装配成模型输入。"""

    def __init__(
        self,
        settings: Settings,
        uploads: UploadStore | None = None,
        profile: ProfileService | None = None,
        media: MediaService | None = None,
    ) -> None:
        self._settings = settings
        self._uploads = uploads
        self._profile = profile
        self._media = media
        # 摘要器按需创建：绝大多数会话永远碰不到摘要阈值，没必要启动就建一个模型
        self.summarizer: Any = None

    # ---------- 附件 ----------

    def attachment_blocks(
        self, attachments: list[str], user_id: str | None = None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """把 upload_id 变成内容块，返回 ``(块列表, 图片 data URL 列表)``。

        两类附件走两条完全不同的路：

        - **图片**变成 ``image_url`` 块，模型直接看图；
        - **文档**变成一个 ``text`` 块，里面是结构摘要加本地路径。不能把 docx 当
          图片塞进去，也不能把整份表格摊成文本——后者几十行就把上下文吃光了，
          而且模型对着长文本心算求和一定会错。给摘要让它「知道有什么」，
          给路径让它「需要精确数字时自己去查」（``query_sheet`` / ``aggregate_sheet``）。

        取不到的附件跳过并记日志，不报错：一张图在传输过程中被删掉（用户点了移除、
        或者磁盘被清理），不该让整轮对话失败——其余几个还能用，模型也会照常回答。

        ``user_id`` 用于归属校验：附件 id 是可传递的凭证，拿到别人的 id 不该
        能把对方的文件塞进自己的上下文。
        """
        if not attachments or self._uploads is None:
            return [], []
        blocks: list[dict[str, Any]] = []
        images: list[str] = []
        for upload_id in attachments[:MAX_ATTACHMENTS]:
            item = self._uploads.get(upload_id, user_id)
            if item is None:
                logger.warning("附件不存在或无权访问，已跳过 | id=%s", upload_id)
                continue
            path = self._uploads.path_of(item)
            if not path.is_file():
                logger.warning("附件文件已丢失，已跳过 | id=%s", upload_id)
                continue
            if item.is_doc:
                blocks.append({"type": "text", "text": self._doc_prompt(item, path)})
            else:
                block = image_block(path, mime_of(item))
                blocks.append(block)
                images.append(block["image_url"]["url"])
        return blocks, images

    def attachment_meta(
        self, ids: list[str], user_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """历史消息里的附件 id → 展示用元数据。

        刷新页面后前端手上只有一串 id，分不清哪个是图片、哪个是文档：图片要缩略图，
        文档要文件名。放在这里一次查完，而不是让前端逐个请求——一条消息最多六个附件，
        逐个查就是六次往返。

        同样做归属校验：越权的附件在历史里直接不显示，而不是给一个点开就 404 的卡片。
        """
        if self._uploads is None or not ids:
            return {}
        meta: dict[str, dict[str, Any]] = {}
        for upload_id in ids:
            item = self._uploads.get(upload_id, user_id)
            if item is None:
                continue
            meta[upload_id] = {
                "kind": item.kind,
                "filename": item.filename,
                "ext": item.ext,
                "bytes": item.bytes,
            }
        return meta

    def attachment_names(self, attachments: list[str] | None) -> list[str]:
        """附件 upload_id → 文件名。工具路由靠扩展名判断要不要绑 docs 组。"""
        if not attachments or self._uploads is None:
            return []
        names = []
        for upload_id in attachments:
            item = self._uploads.get(upload_id)
            if item is not None:
                names.append(item.filename)
        return names

    def history_text(self, message: ChatMessage) -> str:
        """历史消息的正文：用户消息末尾补一行「当时传过哪些文档」。

        文档内容块只在**当轮**注入（见 ``attachment_blocks``），落库的只有
        ``attachments`` 那串 id。于是下一轮模型手上只剩一句「按网点汇总」，
        它不知道文件在哪，就去翻桌面、下载、文档三个目录，最后回一句「源文件
        已找不到」——用户明明刚传过。2026-11-11 实测就是这么卡死的。

        补一行路径只要几十个 token，却能让 ``query_sheet`` / ``aggregate_sheet``
        在后续轮次继续工作。图片不补：一张 1280 的图约 1.5k token，而上一轮的
        回答里已经有模型对它的描述——见 ``build`` 里那段取舍说明。
        """
        if message.role != "user" or not message.attachments or self._uploads is None:
            return message.content
        lines: list[str] = []
        for upload_id in message.attachments:
            item = self._uploads.get(upload_id)
            if item is None or not item.is_doc:
                continue
            path = self._uploads.path_of(item)
            # 文件被清理掉时不要留一条指向空处的路径：模型照着调工具只会拿到
            # 「文件不存在」，然后又开始翻目录
            if not path.is_file():
                continue
            name = item.filename or path.name
            lines.append(f"（本消息附带文件「{name}」，本地路径：{path}）")
        if not lines:
            return message.content
        note = "\n".join(lines)
        return f"{message.content}\n\n{note}" if message.content.strip() else note

    def session_files(self, session: ChatSession) -> str:
        """本会话用户传过的文件清单，**含已被摘要折叠掉的那些**。

        ``history_text`` 只对还在窗口里的消息生效。摘要一推进
        （``summarized_count``），带附件的那条消息就不再进 messages，路径跟着
        消失——而摘要器并不会老老实实抄路径。2026-11-12 实测：摘要里只剩
        「整理26个网点…」，下一轮模型就说「我手上没有源文件名」，让用户把
        Excel 挪到桌面，用户第三次传同一个文件还是这句。

        所以清单按**全量消息**推导，绕开摘要这条会丢信息的路径。代价是每轮
        多几十个 token，比让用户反复重传便宜得多。
        """
        if self._uploads is None:
            return ""
        # 同名文件传多次只留最后一次的路径：用户重传往往是因为以为没传上，
        # 两份内容其实一样，列两行只会让模型反问「是哪一份」。dict 保持首次
        # 出现的顺序，值被后一次覆盖，正好是「位置不变、路径取最新」
        files: dict[str, str] = {}
        for message in session.messages:
            for upload_id in message.attachments or []:
                item = self._uploads.get(upload_id)
                if item is None or not item.is_doc:
                    continue
                path = self._uploads.path_of(item)
                if not path.is_file():
                    continue
                files[item.filename or path.name] = str(path)
                if len(files) >= MAX_SESSION_FILES:
                    break
            if len(files) >= MAX_SESSION_FILES:
                break
        if not files:
            return ""
        lines = [f"- {name}：{path}" for name, path in files.items()]
        return (
            "用户在本会话上传过这些文件，直接用下面的路径读取，"
            "不要让用户重新上传，也不要让他把文件挪到桌面/下载/文档：\n" + "\n".join(lines)
        )

    def _doc_prompt(self, item: Upload, path: Path) -> str:
        """把一份上传的文档压成一段给模型看的说明。

        **路径必须给绝对路径**：模型拿到它才能传给 ``read_document`` /
        ``query_sheet``。给相对路径的话，工具按进程 CWD 解析，打包后 CWD 是 exe
        所在目录，指到别的地方去了。
        """
        name = item.filename or f"未命名{path.suffix}"
        try:
            body = read_doc_text(path, limit=DOC_BLOCKS_IN_PROMPT).to_text()
        except DocError as exc:
            # 旧格式（.doc/.xls）和损坏文件都落在这里。照样把路径告诉模型，
            # 因为读者那句「另存为 .docx 再传」正是用户需要看到的回复
            body = f"（这份文件暂时读不出内容：{exc}）"
        # 明说「这是表格」：光给路径不够，模型会按扩展名猜，看到 .csv 就以为
        # 汇总工具不支持，转头去读全文自己加——实测就是这么错的
        sheet_hint = ""
        if path.suffix.lower() in SHEET_SUFFIXES:
            sheet_hint = "这是一份表格文件，可以直接传给 query_sheet / aggregate_sheet 统计。\n"
        return (
            f"用户上传了文件「{name}」。\n"
            f"本地路径：{path}\n"
            f"{sheet_hint}\n"
            f"{body}\n\n"
            "要精确统计（求和、平均、计数、分组、筛选、排序）时，对上方的本地路径调用 "
            "query_sheet 或 aggregate_sheet，不要凭这份摘要自己算；要看全文用 read_document。"
        )

    # ---------- 提示词与消息 ----------

    async def system_prompt(self, session: ChatSession, explicit_skill: str, user_id: str) -> str:
        """核心规则 + 技能清单 + 显式技能步骤 + 长期偏好。

        技能清单只放名字和一句话，正文等模型调用 use_skill 或用户显式选中时再进提示词：
        四个技能的完整步骤合起来上千 token，每轮都塞进去和全量绑工具是同一个错误。
        """
        # 提示词按本会话选定的风格组装：核心规则 + 领域规则（旅行类才有）+ 说话方式。
        # 「闲聊时别推行程」不能靠加一句规则——实测那只是概率改善，
        # 得让闲聊型人格根本拿不到旅行规则，见 app/agent/prompts.py 的说明。
        system_text = build_system_prompt(session.persona)
        catalog = catalog_block()
        if catalog:
            system_text = f"{system_text}\n\n{catalog}"
        if explicit_skill:
            # 用户明确选了技能，就把步骤直接给到——这是「显式」通道应有的确定性，
            # 不该再让模型多花一轮去 use_skill
            skill_block = instructions_block(explicit_skill)
            if skill_block:
                system_text = f"{system_text}\n\n{skill_block}"
        if self._profile is not None:
            block = await self._profile.render_block(user_id)
            if block:
                system_text = f"{system_text}\n\n{block}"
        return system_text

    async def build(
        self,
        session: ChatSession,
        *,
        user_id: str,
        model_text: str,
        attachments: list[str],
        explicit_skill: str,
    ) -> TurnPrompt:
        """装配这一轮的完整输入；顺带按预算折叠历史。

        顺序不能改：摘要先折叠，事实块再拼进去——摘要是对对话的压缩，
        不该把「这一轮任务成功了几张」这种瞬时状态卷进长期记忆。
        """
        system_text = await self.system_prompt(session, explicit_skill, user_id)
        await self.refresh_summary(session, system_text)
        # 文档的路径要跟着历史走（history_text），否则下一轮模型不知道文件在哪；
        # 图片只挂当前轮，理由见下面那段
        history = [(item.role, self.history_text(item)) for item in session.messages]
        facts = await self.turn_facts(session)
        prompt_text = f"{system_text}\n\n{facts}" if facts else system_text
        # 文件清单要进提示词而不是历史：历史会被摘要折叠，折叠之后就只剩
        # 一段散文，路径没了。见 session_files 里那次实测
        files = self.session_files(session)
        if files:
            prompt_text = f"{prompt_text}\n\n{files}"
        messages = compose_messages(prompt_text, session.summary, history, session.summarized_count)

        # 图片只挂**当前轮**。每轮重发会让 token 随轮数线性膨胀（一张 1280 的图约 1.5k token），
        # 而用户接着追问的多半还是同一张图——上一轮的回答里已经有模型对它的描述。
        # 代价是「隔一轮再问图里的细节」会问不到，这是刻意选的：宁可让用户重发一次，
        # 也不要在第 10 轮还替第 1 轮的图付钱。
        blocks, images = self.attachment_blocks(attachments, user_id)
        if blocks:
            # 只发了附件没写字是合法用法。提示语要区分图文，否则模型会对着
            # 一份 xlsx 说「这张图里是……」
            has_doc = any(block.get("type") == "text" for block in blocks)
            hint = "请看我上传的文件。" if has_doc else "请看这张图。"
            messages[-1] = HumanMessage(
                content=[{"type": "text", "text": model_text or hint}, *blocks]
            )
        # 同一批图片还要能给生图工具当底图：用户说「在我这张图上加个东西」时，
        # 模型看不见上传 id，也没法把图交给上游，只能靠这里转成 data URL 递下去。
        # 只取前两张——上游单次最多吃两张参考图，再多只是把请求体撑大
        return TurnPrompt(
            system_text=system_text,
            messages=messages,
            refs=images[:MAX_REF_IMAGES],
            history=history,
        )

    async def turn_facts(self, session: ChatSession) -> str:
        """查证「模型看不到但真实发生了的事」，写成一段事实注入提示词。

        这一步存在的理由是一次真实故障：模型连着三轮说「生图服务卡住了」，
        而那几个任务全部 succeeded。它拿不到任务终态，只能从「用户说没看到图」
        反推「任务失败」——推断在它的信息集里是合理的，只是事实是错的。
        事实由系统查好喂给它，它就不需要猜了。

        查询在这里、拼文字在 ``app/services/facts.py``：那边是纯函数，
        可以被单测钉住，不需要 mock 一个 Redis 或生图服务。
        """
        media: list[MediaFact] = []
        if self._media is not None:
            for task_id in recent_task_ids(session.messages):
                task = await self._media.get(task_id)
                if task is None:
                    # 任务过期或已被清理。不编一个状态出来，
                    # 让它从列表里消失比说「状态未知」更诚实
                    continue
                media.append(
                    MediaFact(
                        task_id=task.id,
                        status=task.status.value,
                        error=task.error,
                    )
                )
        return build_turn_facts(media, last_warnings(session.messages))

    async def refresh_summary(self, session: ChatSession, system_prompt: str) -> None:
        """按预算决定是否把历史折叠进摘要。

        摘要失败时不推进 summarized_count，那批消息会继续留在窗口里——
        宁可多占一点上下文，也不能因为摘要失败而丢掉记忆。
        """
        # 用带路径的正文：摘要一旦把这段折叠掉，路径就只能靠摘要本身带下去，
        # 而摘要器只抄它看到的东西
        history = [(item.role, self.history_text(item)) for item in session.messages]
        plan = plan_fold(
            system_prompt=system_prompt,
            summary=session.summary,
            history=history,
            summarized_count=session.summarized_count,
            max_tokens=self._settings.max_context_tokens,
        )
        if not plan.needed:
            return

        if self.summarizer is None:
            self.summarizer = build_chat_model(self._settings)
        try:
            session.summary = await make_summary(self.summarizer, session.summary, plan.fold)
        except Exception as exc:  # noqa: BLE001 - 摘要失败不能影响正常回答
            logger.warning("上下文摘要生成失败，本轮沿用原摘要：%s", exc)
            return
        session.summarized_count = plan.new_summarized_count

    @staticmethod
    def trim(session: ChatSession) -> None:
        """裁剪超长会话。

        裁掉的消息若已被折叠进摘要，需同步回退计数，保持下标一致。
        """
        limit = MAX_TURNS * 2
        excess = len(session.messages) - limit
        if excess > 0:
            del session.messages[:excess]
            session.summarized_count = max(0, session.summarized_count - excess)
