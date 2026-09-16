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

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from planrelay_bridge import cli, worker
from planrelay_bridge.auth import Settings
from planrelay_bridge.repository import github_from_url, github_name
from planrelay_bridge.server import create_app
from planrelay_bridge.store import Store


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Example/Project.git",
        "git@github.com:Example/Project.git",
        "ssh://git@github.com/Example/Project.git",
        "https://github.com/Example/Project",
    ],
)
def test_github_identity_is_canonical(url: str) -> None:
    assert github_from_url(url) == "example/project"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/project",
        "https://github.com.evil.test/example/project",
        "https://secret@github.com/example/project",
        "https://github.com/example/project?token=secret",
        "https://github.com/example/project#secret",
        "https://github.com:8443/example/project",
        "ssh://evil@github.com/example/project",
        "git@github.com:example/../project",
        "https://github.com/example/%70roject",
        "https://github.com/example/project/extra",
        "https://github.com/example/..",
        "https://github.com/example/project\n",
    ],
)
def test_github_identity_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        github_from_url(url)


@pytest.mark.parametrize("name", ["../repo", "owner/repo/extra", "o/..", "o/r?x"])
def test_github_name_rejects_invalid_identity(name: str) -> None:
    with pytest.raises(ValueError):
        github_name(name)


@pytest.fixture
def config(tmp_path: Path) -> worker.WorkerConfig:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("print('hello')\n")
    for arguments in (
        ["init", "-q"],
        ["add", "app.py"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "baseline",
        ],
        ["remote", "add", "origin", "https://github.com/Example/Project.git"],
    ):
        subprocess.run(["git", "-C", str(project), *arguments], check=True)
    return worker.WorkerConfig(
        bridge_url="http://127.0.0.1:8787",
        worker_id="worker-test",
        token="x" * 32,
        project_id="example",
        project=project,
        files=["app.py"],
        artifacts=tmp_path / "artifacts",
    )


def test_inspect_does_not_export_local_paths_or_sources(
    config: worker.WorkerConfig,
) -> None:
    inspected = worker.inspect_project(config.project)
    assert inspected["github_repository"] == "example/project"
    assert inspected["clean"] is True
    assert len(inspected["git_head"]) == 40
    assert str(config.project) not in json.dumps(inspected)
    assert "print(" not in json.dumps(inspected)
    (config.project / "app.py").write_text("changed")
    assert worker.inspect_project(config.project)["clean"] is False


def test_binding_checks_before_snapshot_and_detects_origin_drift(
    config: worker.WorkerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound = worker.WorkerConfig(
        **{**config.model_dump(), "github_repository": "Example/Project"}
    )
    assert bound.github_repository == "example/project"
    baseline = worker.snapshot(bound)
    assert baseline == worker.snapshot(config)
    real_read = worker.read_file

    def drift(root: Path, filename: str, *args: object, **kwargs: object) -> bytes:
        value = real_read(root, filename)
        if filename == "app.py":
            worker._git(
                config.project,
                "remote",
                "set-url",
                "origin",
                "https://github.com/other/project",
            )
        return value

    monkeypatch.setattr(worker, "read_file", drift)
    with pytest.raises(ValueError, match="bound GitHub"):
        worker.snapshot(bound)


def test_unsafe_git_config_is_rejected_before_git_command(
    config: worker.WorkerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker._git(config.project, "config", "include.path", "/private/no-follow")
    monkeypatch.setattr(
        worker, "_git", lambda *_args, **_kwargs: pytest.fail("Git must not run")
    )
    with pytest.raises(ValueError, match="Unsafe"):
        worker.snapshot(config)
    with pytest.raises(ValueError, match="Unsafe"):
        worker.inspect_project(config.project)


def test_bound_snapshot_requires_single_matching_origin(
    config: worker.WorkerConfig,
) -> None:
    bound = worker.WorkerConfig(
        **{**config.model_dump(), "github_repository": "other/project"}
    )
    with pytest.raises(ValueError):
        worker.snapshot(bound)
    worker._git(
        config.project,
        "config",
        "--add",
        "remote.origin.url",
        "https://github.com/other/project",
    )
    with pytest.raises(ValueError):
        worker.inspect_project(config.project)
    worker._git(config.project, "remote", "remove", "origin")
    with pytest.raises(ValueError):
        worker.snapshot(bound)
    assert worker.inspect_project(config.project)["github_repository"] is None
    assert worker.snapshot(config)["project_id"] == "example"


def test_sync_only_registers_and_never_executes(
    config: worker.WorkerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=json.loads(request.content))

    monkeypatch.setattr(
        worker, "run_once", lambda *_args, **_kwargs: pytest.fail("No model/claim")
    )
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        baseline = worker.sync(config, client)
        assert baseline["revision"] == worker.snapshot(config)["revision"]
        assert calls == ["/worker/projects"]
        (config.project / "app.py").write_text("changed")
        with pytest.raises(ValueError, match="changes"):
            worker.sync(config, client)
        assert calls == ["/worker/projects"]


@pytest.mark.skipif(
    os.getenv("PLANPORT_REAL_SYNC") != "1", reason="Opt-in real local HTTP/MCP sync"
)
def test_real_cli_sync_mcp_roundtrip_without_model(
    config: worker.WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    web_token = secrets.token_urlsafe(32)
    settings = Settings(
        public_url=origin + "/mcp",
        issuer=origin,
        owner_subject="fixture-owner",
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
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    private_config = private_dir / "worker.json"
    config.artifacts.mkdir(mode=0o700)
    monkeypatch.setattr(
        worker,
        "run_once",
        lambda *_args, **_kwargs: pytest.fail("Sync must not execute models"),
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started

        async def exercise() -> None:
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + web_token}, trust_env=False
            ) as http_client:
                async with streamable_http_client(
                    origin + "/mcp", http_client=http_client
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        paired = await session.call_tool("pair_device", {})
                        assert paired.structuredContent
                        pair_arguments = [
                            "pair",
                            "--bridge",
                            origin,
                            "--project",
                            str(config.project),
                            "--project-id",
                            config.project_id,
                            "--github",
                            "Example/Project",
                            "--file",
                            "app.py",
                            "--artifacts",
                            str(config.artifacts),
                            "--config",
                            str(private_config),
                            "--code=" + paired.structuredContent["code"],
                        ]
                        assert await asyncio.to_thread(cli.main, pair_arguments) == 0
                        selected = cli.load_config(private_config)
                        assert selected.github_repository == "example/project"
                        assert not selected.experimental_execution
                        before = await session.call_tool(
                            "get_project_context", {"project_id": config.project_id}
                        )
                        assert before.structuredContent
                        (config.project / "app.py").write_text("print('updated')\n")
                        worker._git(config.project, "add", "app.py")
                        worker._git(
                            config.project,
                            "-c",
                            "user.name=Test",
                            "-c",
                            "user.email=test@example.test",
                            "commit",
                            "-qm",
                            "updated baseline",
                        )
                        assert (
                            await asyncio.to_thread(
                                cli.main, ["sync", "--config", str(private_config)]
                            )
                            == 0
                        )
                        after = await session.call_tool(
                            "get_project_context", {"project_id": config.project_id}
                        )
                        assert after.structuredContent
                        assert (
                            before.structuredContent["revision"]
                            != after.structuredContent["revision"]
                        )
                        assert "updated" in after.structuredContent["context"]
                        worker._git(
                            config.project,
                            "remote",
                            "set-url",
                            "origin",
                            "https://github.com/other/project",
                        )
                        assert (
                            await asyncio.to_thread(
                                cli.main, ["sync", "--config", str(private_config)]
                            )
                            == 1
                        )
                        unchanged = await session.call_tool(
                            "get_project_context", {"project_id": config.project_id}
                        )
                        assert unchanged.structuredContent == after.structuredContent
                        assert not list(config.artifacts.iterdir())

        asyncio.run(exercise())
        output = capsys.readouterr()
        assert web_token not in output.out + output.err
        assert "print(" not in output.out
    finally:
        server.should_exit = True
        thread.join(timeout=10)
