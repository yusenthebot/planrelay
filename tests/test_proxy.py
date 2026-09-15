from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from planrelay_bridge import worker
from planrelay_bridge.proxy import create_proxy
from planrelay_bridge.worker import WorkerConfig


@pytest.fixture
def config(tmp_path: Path) -> WorkerConfig:
    project = tmp_path / "project"
    project.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return WorkerConfig(
        bridge_url="https://relay.example",
        worker_id="worker",
        token="x" * 32,
        project_id="example",
        project=project,
        artifacts=artifacts,
        files=["app.py"],
    )


def invoke(config: WorkerConfig, name: str, **args: Any) -> Any:
    server = create_proxy(config)
    tool = server._tool_manager.get_tool(name)
    assert tool is not None
    return tool.fn(**args)


def test_three_tools_read_only(config: WorkerConfig) -> None:
    tools = asyncio.run(create_proxy(config).list_tools())
    assert {tool.name for tool in tools} == {
        "list_projects",
        "get_project_context",
        "get_run",
    }
    assert all(tool.annotations.readOnlyHint for tool in tools)


def test_list_filters_fixed_local_project(
    config: WorkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        worker,
        "_post",
        lambda *args: (
            calls.append(args[2:])
            or {
                "projects": [{"project_id": "other"}, {"project_id": "example"}],
            }
        ),
    )
    assert invoke(config, "list_projects") == [{"project_id": "example"}]
    assert calls == [
        (
            "/worker/relay",
            {
                "action": "list_projects",
                "project_id": "example",
            },
        )
    ]


def test_project_cross_scope_fails_before_remote_call(
    config: WorkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "_post", lambda *_: pytest.fail("no remote call"))
    with pytest.raises(ToolError):
        invoke(config, "get_project_context", project_id="other")


@pytest.mark.parametrize(
    "name,args,response",
    [
        ("get_project_context", {"project_id": "example"}, {"project_id": "other"}),
        ("get_run", {"run_id": "run"}, {"id": "run", "project_id": "other"}),
        ("get_run", {"run_id": "run"}, {"id": "different", "project_id": "example"}),
        ("list_projects", {}, {"projects": ["invalid"]}),
    ],
)
def test_response_scope_and_shape_checked(
    config: WorkerConfig,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
    response: dict[str, Any],
) -> None:
    monkeypatch.setattr(worker, "_post", lambda *_: response)
    with pytest.raises(ToolError):
        invoke(config, name, **args)


def test_scoped_run_success_and_redacted_remote_error(
    config: WorkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "_post",
        lambda *_: {
            "id": "run",
            "project_id": "example",
            "state": "succeeded",
        },
    )
    assert invoke(config, "get_run", run_id="run")["state"] == "succeeded"
    monkeypatch.setattr(
        worker,
        "_post",
        lambda *_: (_ for _ in ()).throw(httpx.ConnectError("secret-token-value")),
    )
    with pytest.raises(ToolError) as error:
        invoke(config, "get_run", run_id="run")
    assert "secret-token-value" not in str(error.value)
