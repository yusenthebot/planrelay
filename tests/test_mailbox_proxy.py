from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from planrelay_bridge import mailbox
from planrelay_bridge.mailbox_proxy import create_proxy


def test_companion_is_read_only_and_project_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: Any = SimpleNamespace(project_id="demo", repository="owner/inbox")
    server = create_proxy(config)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "list_projects",
        "get_project_context",
        "get_run",
        "get_web_prompt",
    }
    assert all(tool.annotations.readOnlyHint for tool in tools)
    monkeypatch.setattr(mailbox, "read_context", lambda _: {"project_id": "demo"})
    tool = server._tool_manager.get_tool("get_project_context")
    assert tool is not None
    assert tool.fn("demo") == {"project_id": "demo"}
    with pytest.raises(ToolError):
        tool.fn("other")


def test_remote_errors_do_not_expose_provider_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: Any = SimpleNamespace(project_id="demo", repository="owner/inbox")
    monkeypatch.setattr(
        mailbox,
        "get_run",
        lambda *_: (_ for _ in ()).throw(ValueError("private-token")),
    )
    tool = create_proxy(config)._tool_manager.get_tool("get_run")
    assert tool is not None
    with pytest.raises(ToolError) as error:
        tool.fn(1)
    assert "private-token" not in str(error.value)


def test_read_only_companion_delivers_the_three_prompt_pack() -> None:
    config: Any = SimpleNamespace(project_id="demo", repository="owner/inbox")
    tool = create_proxy(config)._tool_manager.get_tool("get_web_prompt")
    assert tool is not None
    assert set(tool.fn()["prompts"]) == {
        "codex_connect",
        "web_connect",
        "codex_receive",
    }
