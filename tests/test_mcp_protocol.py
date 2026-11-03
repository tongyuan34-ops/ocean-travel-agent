"""MCP 协议层：JSON-RPC 分帧、请求元数据、版本协商。

规范 2026-07-28 把协议改成了无状态，元数据从「握手时交换一次」变成
「每个请求自带」。这一层的错误全是「格式对了但语义错了」，只能靠断言具体
错误码来抓——返回一个空结果和返回 -32602，对客户端是两种完全不同的处境。
"""

from __future__ import annotations

import json

import pytest

from app.mcp import protocol


def test_encode_keeps_chinese_readable():
    """中文不转义：抓包和日志里能直接看懂，排障成本差很多。"""
    assert "预算" in protocol.encode({"text": "预算"})


def test_encode_escapes_newlines_so_framing_holds():
    """一条消息一行是分帧的全部约定，正文里的换行必须被转义。"""
    line = protocol.encode({"text": "第一行\n第二行"})

    assert "\n" not in line
    assert json.loads(line)["text"] == "第一行\n第二行"


def test_decode_roundtrip():
    message = {"jsonrpc": "2.0", "id": 7, "method": "ping"}

    assert protocol.decode(protocol.encode(message)) == message


@pytest.mark.parametrize(
    "line,code",
    [
        ("{不是 json", protocol.PARSE_ERROR),
        ("[]", protocol.INVALID_REQUEST),
        ('{"id":1,"method":"ping"}', protocol.INVALID_REQUEST),
    ],
)
def test_decode_rejects_bad_messages(line: str, code: int):
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.decode(line)

    assert exc.value.code == code


def test_parse_meta_reads_all_fields():
    meta = protocol.parse_meta(
        {
            "_meta": {
                protocol.META_VERSION: "2026-07-28",
                protocol.META_CLIENT_INFO: {"name": "x", "version": "1"},
                protocol.META_CLIENT_CAPABILITIES: {},
            }
        }
    )

    assert meta is not None
    assert meta.protocol_version == "2026-07-28"
    assert meta.client_info == {"name": "x", "version": "1"}


def test_parse_meta_absent_means_legacy_candidate():
    """没有 _meta 不等于错误：可能是老版客户端，交给上层按会话语义处理。"""
    assert protocol.parse_meta({}) is None
    assert protocol.parse_meta(None) is None


@pytest.mark.parametrize(
    "meta",
    [
        {protocol.META_CLIENT_CAPABILITIES: {}},  # 缺版本
        {protocol.META_VERSION: "2026-07-28"},  # 缺能力
        {protocol.META_VERSION: "", protocol.META_CLIENT_CAPABILITIES: {}},  # 空版本
    ],
)
def test_parse_meta_requires_version_and_capabilities(meta: dict):
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_meta({"_meta": meta})

    assert exc.value.code == protocol.INVALID_PARAMS


def test_client_info_is_optional():
    """规范里 clientInfo 是 SHOULD，不是 MUST；当必填会挡掉合法客户端。"""
    meta = protocol.parse_meta(
        {
            "_meta": {
                protocol.META_VERSION: "2026-07-28",
                protocol.META_CLIENT_CAPABILITIES: {},
            }
        }
    )

    assert meta is not None
    assert meta.client_info == {}


def test_unknown_version_lists_what_we_support():
    """-32022 必须带上 supported，否则客户端没有任何降级依据。"""
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.require_supported("1900-01-01")

    assert exc.value.code == protocol.UNSUPPORTED_PROTOCOL_VERSION
    assert protocol.PROTOCOL_VERSION in exc.value.data["supported"]
    assert exc.value.data["requested"] == "1900-01-01"


def test_error_message_shape():
    payload = protocol.error_message(3, protocol.METHOD_NOT_FOUND, "没有这个方法")

    assert payload["error"]["code"] == protocol.METHOD_NOT_FOUND
    assert payload["id"] == 3


def test_call_result_marks_business_failure_as_is_error():
    """工具执行失败走 isError，不是 JSON-RPC error——后者模型看不到内容。"""
    result = protocol.call_result("TOOL_FAILED：x", is_error=True)

    assert result["isError"] is True
    assert result["resultType"] == protocol.RESULT_COMPLETE
    assert result["content"][0]["text"] == "TOOL_FAILED：x"


def test_supported_versions_start_with_current():
    """第一条必须是当前实现版本，客户端通常取第一个能用的。"""
    assert protocol.SUPPORTED_VERSIONS[0] == protocol.PROTOCOL_VERSION
