"""Opt-in real local HTTP/MCP/Codex test; never touches business repositories."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import SecretStr

from planrelay_bridge.auth import Settings
from planrelay_bridge.server import create_app
from planrelay_bridge.store import Store
from planrelay_bridge.worker import WorkerConfig, pair, run_once, snapshot


@pytest.mark.skipif(
    os.getenv("PLANRELAY_REAL_MODEL") != "1",
    reason="Opt-in authenticated model execution",
)
def test_real_http_model_result_roundtrip(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# Synthetic relay fixture\n", encoding="utf-8")
    for arguments in [
        ("init",),
        ("add", "README.md"),
        (
            "-c",
            "user.name=Relay Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ),
    ]:
        subprocess.run(
            ["git", "-C", str(project), *arguments],
            check=True,
            capture_output=True,
            timeout=10,
        )  # nosec B603 B607
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    web_token = secrets.token_urlsafe(32)
    settings = Settings(
        public_url=origin + "/mcp",
        issuer=origin,
        owner_subject="synthetic-owner",
        local_token=SecretStr(web_token),
        database=tmp_path / "relay.sqlite",
        port=port,
    )
    store = Store(settings.database)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, store=store),
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started

        async def workflow() -> None:
            async with streamablehttp_client(
                origin + "/mcp", headers={"Authorization": "Bearer " + web_token}
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    paired = await session.call_tool("pair_device", {})
                    assert paired.structuredContent
                    device = await asyncio.to_thread(
                        pair, origin, paired.structuredContent["code"]
                    )
                    config = WorkerConfig(
                        bridge_url=origin,
                        worker_id=device["worker_id"],
                        token=SecretStr(device["token"]),
                        project_id="fixture",
                        project=project,
                        files=["README.md"],
                        artifacts=artifacts,
                        timeout_seconds=120,
                        model="gpt-6-astra",
                        experimental_execution=True,
                    )
                    context = snapshot(config)
                    store.register_project("synthetic-owner", config.worker_id, context)
                    submitted = await session.call_tool(
                        "submit_plan",
                        {
                            "project_id": "fixture",
                            "request": (
                                "Create hello.txt containing exactly relay-ok. "
                                "Do not change other files. Verify its content "
                                "and report exactly relay-ok."
                            ),
                            "plan": (
                                "Use a workspace-only file edit, verify hello.txt "
                                "locally. No Git commands, network, dependencies "
                                "or commits."
                            ),
                            "revision": context["revision"],
                            "idempotency_key": "real-smoke",
                        },
                    )
                    assert submitted.structuredContent
                    result = await asyncio.to_thread(run_once, config)
                    assert result and result["state"] == "succeeded", result
                    received = await session.call_tool(
                        "get_run", {"run_id": submitted.structuredContent["id"]}
                    )
                    assert received.structuredContent
                    final = received.structuredContent
                    assert final["state"] == "succeeded"
                    assert final["result"]["changed_files"] == ["hello.txt"]
                    assert "relay-ok" in final["result"]["patch"]
                    assert not (project / "hello.txt").exists()
                    repeated = await session.call_tool(
                        "submit_plan",
                        {
                            "project_id": "fixture",
                            "request": submitted.structuredContent["request"],
                            "plan": submitted.structuredContent["plan"],
                            "revision": context["revision"],
                            "idempotency_key": "real-smoke",
                        },
                    )
                    assert repeated.structuredContent
                    assert repeated.structuredContent["id"] == final["id"]
                    assert await asyncio.to_thread(run_once, config) is None
                    queued = await session.call_tool(
                        "submit_plan",
                        {
                            "project_id": "fixture",
                            "request": "No changes; cancellation fixture",
                            "plan": "Wait for cancellation",
                            "revision": context["revision"],
                            "idempotency_key": "cancel-smoke",
                        },
                    )
                    assert queued.structuredContent
                    cancelled = await session.call_tool(
                        "cancel_run", {"run_id": queued.structuredContent["id"]}
                    )
                    assert cancelled.structuredContent
                    assert cancelled.structuredContent["state"] == "cancelled"
                    assert await asyncio.to_thread(run_once, config) is None
                    private = tmp_path / "worker.json"
                    values = config.model_dump(mode="json")
                    values["token"] = config.token.get_secret_value()
                    private.write_text(json.dumps(values), encoding="utf-8")
                    private.chmod(0o600)
                    launcher = Path(
                        os.environ.get(
                            "PLANPORT_TEST_LAUNCHER",
                            str(
                                Path(__file__).resolve().parents[1]
                                / "bin"
                                / "gpt-connector-launcher"
                            ),
                        )
                    )
                    parameters = StdioServerParameters(
                        command=str(launcher),
                        env={
                            "PLANRELAY_CONFIG": str(private),
                            "PATH": os.environ["PATH"],
                        },
                    )
                    async with stdio_client(parameters) as (proxy_read, proxy_write):
                        async with ClientSession(proxy_read, proxy_write) as proxy:
                            await proxy.initialize()
                            tools = (await proxy.list_tools()).tools
                            assert {tool.name for tool in tools} == {
                                "list_projects",
                                "get_project_context",
                                "get_run",
                            }
                            proxied = await proxy.call_tool(
                                "get_run", {"run_id": final["id"]}
                            )
                            assert not proxied.isError
                            assert proxied.structuredContent
                            assert proxied.structuredContent["state"] == "succeeded"

        asyncio.run(workflow())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
