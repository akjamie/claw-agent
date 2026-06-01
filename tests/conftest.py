"""Shared pytest fixtures for claw-gateway tests."""

import pytest


@pytest.fixture
def weixin_token() -> str:
    """Fixed token string for WeChat signature verification tests."""
    return "test_token_abc123"


@pytest.fixture
def sample_text_xml() -> bytes:
    """Valid WeChat text message XML bytes."""
    return (
        b"<xml>"
        b"<ToUserName><![CDATA[gh_test_account]]></ToUserName>"
        b"<FromUserName><![CDATA[oUser123456]]></FromUserName>"
        b"<CreateTime>1348831860</CreateTime>"
        b"<MsgType><![CDATA[text]]></MsgType>"
        b"<Content><![CDATA[Hello World]]></Content>"
        b"<MsgId>1234567890123456</MsgId>"
        b"</xml>"
    )


@pytest.fixture
def sample_event_xml() -> bytes:
    """Valid WeChat event XML bytes (subscribe event)."""
    return (
        b"<xml>"
        b"<ToUserName><![CDATA[gh_test_account]]></ToUserName>"
        b"<FromUserName><![CDATA[oUser123456]]></FromUserName>"
        b"<CreateTime>1348831860</CreateTime>"
        b"<MsgType><![CDATA[event]]></MsgType>"
        b"<Event><![CDATA[subscribe]]></Event>"
        b"<EventKey><![CDATA[]]></EventKey>"
        b"</xml>"
    )


@pytest.fixture
def gateway_config_dict() -> dict:
    """Sample gateway config dict with one platform and one MCP server."""
    return {
        "host": "0.0.0.0",
        "port": 18080,
        "platforms": {
            "weixin": {
                "enabled": True,
                "app_id": "wx_test_app_id",
                "app_secret": "wx_test_secret",
                "token": "test_token_abc123",
                "encoding_aes_key": "",
            }
        },
        "mcp_servers": {
            "sqlite": {
                "command": "uvx",
                "args": ["mcp-server-sqlite", "--db-path", "./data.db"],
                "env": {},
                "enabled": True,
            }
        },
    }
