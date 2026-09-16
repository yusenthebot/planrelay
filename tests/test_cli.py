from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import httpx
import pytest

from planrelay_bridge import cli, worker
from planrelay_bridge.worker import WorkerConfig


def test_public_command_identity() -> None:
    assert cli.parser().prog == "gpt-connector"


def test_github_start_write_gate_runs_before_binding_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from planrelay_bridge import mailbox

    monkeypatch.setattr(mailbox, "load_config", lambda _: pytest.fail("no read"))
    assert cli.main(["github", "start", "--issue", "2"]) == 1


def test_github_start_dispatches_without_running_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from planrelay_bridge import mailbox, mailbox_cli

    monkeypatch.setattr(mailbox, "load_config", lambda _: SimpleNamespace())
    monkeypatch.setattr(worker, "run", lambda *_: pytest.fail("no model"))
    monkeypatch.setattr(
        mailbox,
        "start_task",
        lambda _c, issue: {"issue": issue, "branch": "gpt-connector/task-2"},
        raising=False,
    )
    args = cli.parser().parse_args(["github", "start", "--issue", "2", "--allow-write"])
    assert mailbox_cli.run(args)["branch"] == "gpt-connector/task-2"


def test_github_prompt_returns_three_bound_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from planrelay_bridge import mailbox, mailbox_cli

    monkeypatch.setattr(
        mailbox,
        "load_config",
        lambda _: SimpleNamespace(repository="owner/inbox", project_id="demo"),
    )
    value = mailbox_cli.run(cli.parser().parse_args(["github", "prompt"]))
    assert set(value["prompts"]) == {"codex_connect", "web_connect", "codex_receive"}


def test_github_setup_requires_explicit_write_permission() -> None:
    args = cli.parser().parse_args(
        [
            "github",
            "setup",
            "--repository",
            "owner/inbox",
            "--project",
            "/tmp",
            "--project-id",
            "demo",
            "--file",
            "README.md",
        ]
    )
    assert args.allow_write is False
    assert args.file == ["README.md"]


def test_github_receive_and_result_arguments() -> None:
    args = cli.parser().parse_args(["github", "next", "--issue", "1"])
    assert args.issue == 1
    args = cli.parser().parse_args(
        [
            "github",
            "result",
            "--issue",
            "1",
            "--body-sha256",
            "a" * 64,
            "--summary-file",
            "/tmp/result.md",
            "--state",
            "succeeded",
        ]
    )
    assert args.allow_write is False


def test_github_write_gate_blocks_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from planrelay_bridge import mailbox

    monkeypatch.setattr(mailbox, "setup", lambda *_a, **_k: pytest.fail("no write"))
    assert (
        cli.main(
            [
                "github",
                "setup",
                "--repository",
                "owner/inbox",
                "--project",
                "/tmp",
                "--project-id",
                "demo",
                "--file",
                "README.md",
            ]
        )
        == 1
    )


@pytest.mark.parametrize("action", ["sync", "status", "next", "prompt"])
def test_github_commands_dispatch_without_starting_worker(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from planrelay_bridge import mailbox, mailbox_cli

    config = SimpleNamespace(repository="owner/inbox", project_id="demo")
    monkeypatch.setattr(mailbox, "load_config", lambda _: config)
    monkeypatch.setattr(worker, "run", lambda *_: pytest.fail("no model"))
    monkeypatch.setattr(mailbox, "sync", lambda _: {"revision": "a" * 64})
    monkeypatch.setattr(mailbox, "status", lambda _: {"issues": []})
    monkeypatch.setattr(mailbox, "next_task", lambda _c, i: {"issue": i})
    arguments = ["github", action]
    if action == "sync":
        arguments += ["--allow-write"]
    elif action == "next":
        arguments += ["--issue", "1"]
    value = mailbox_cli.run(cli.parser().parse_args(arguments))
    assert isinstance(value, dict)


def test_github_result_reads_only_bounded_selected_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from planrelay_bridge import mailbox, mailbox_cli

    summary = tmp_path / "summary.md"
    summary.write_text("Actual validation: synthetic greeting checked.")
    monkeypatch.setattr(mailbox, "load_config", lambda _: SimpleNamespace())
    monkeypatch.setattr(mailbox, "publish_result", lambda *args: {"state": args[-1]})
    args = cli.parser().parse_args(
        [
            "github",
            "result",
            "--issue",
            "1",
            "--body-sha256",
            "a" * 64,
            "--summary-file",
            str(summary),
            "--state",
            "succeeded",
            "--allow-write",
        ]
    )
    assert mailbox_cli.run(args) == {"state": "succeeded"}
    summary.write_text("sk-" + "a" * 40)
    with pytest.raises(ValueError):
        mailbox_cli.run(args)


def test_explicit_bridge_config_takes_priority_over_default_mailbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from planrelay_bridge import mailbox, mailbox_cli, proxy

    path = tmp_path / "mailbox.json"
    path.write_text("{}")
    monkeypatch.setattr(mailbox_cli, "default_path", lambda: path)
    monkeypatch.setattr(mailbox, "load_config", lambda _: pytest.fail("wrong mode"))
    monkeypatch.setattr(cli, "load_config", lambda _: SimpleNamespace())
    calls = []
    monkeypatch.setattr(
        proxy,
        "create_proxy",
        lambda _: SimpleNamespace(run=lambda **kwargs: calls.append(kwargs)),
    )
    assert cli.main(["mcp", "--config", str(cli.default_config_path())]) == 0
    assert calls == [{"transport": "stdio"}]


def test_conflicting_mcp_configs_rejected_before_loading() -> None:
    assert cli.main(["mcp", "--config", "/tmp/a", "--github-config", "/tmp/b"]) == 1


def test_plugin_identity_and_legacy_commands() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text())
    assert manifest["name"] == "gpt-connector"
    assert manifest["interface"]["displayName"] == "GPT Connector"
    mcp = json.loads((root / ".mcp.json").read_text())["mcpServers"]
    assert set(mcp) == {"gpt-connector"}
    launcher = root / mcp["gpt-connector"]["command"]
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert (root / "skills/gpt-connector/SKILL.md").is_file()
    project = tomllib.loads((root / "pyproject.toml").read_text())
    for command in ("gpt-connector", "planport", "planrelay"):
        assert project["project"]["scripts"][command] == "planrelay_bridge.cli:main"


def test_inspect_and_sync_parser_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "worker.json"
    monkeypatch.setenv("PLANRELAY_CONFIG", str(path))
    inspect = cli.parser().parse_args(["inspect", "--project", str(tmp_path)])
    assert inspect.project == tmp_path
    assert cli.parser().parse_args(["sync"]).config == path
    assert cli.parser().parse_args(["sync", "--config", str(path)]).config == path
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["inspect"])


def test_inspect_emits_only_project_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = {"git_head": "a" * 40, "github_repository": "owner/repo", "clean": True}
    calls: list[Path] = []
    monkeypatch.setattr(
        worker,
        "inspect_project",
        lambda project: calls.append(project) or value,
        raising=False,
    )
    monkeypatch.setattr(cli, "load_config", lambda _: pytest.fail("no config needed"))
    assert cli.main(["inspect", "--project", str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == value
    assert str(tmp_path) not in output.out
    assert output.err == ""
    assert calls == [tmp_path]


@pytest.fixture
def config(tmp_path: Path) -> WorkerConfig:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("print('hello')\n")
    for args in (
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
    ):
        subprocess.run(["git", "-C", str(project), *args], check=True)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return WorkerConfig(
        bridge_url="http://127.0.0.1:8787",
        worker_id="worker-test",
        token="x" * 32,
        project_id="example",
        project=project,
        files=["app.py"],
        artifacts=artifacts,
    )


def save(config: WorkerConfig, path: Path) -> None:
    value = config.model_dump(mode="json")
    value["token"] = config.token.get_secret_value()
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def pair_args(config: WorkerConfig, path: Path) -> list[str]:
    return [
        "pair",
        "--bridge",
        config.bridge_url,
        "--project",
        str(config.project),
        "--project-id",
        config.project_id,
        "--file",
        "app.py",
        "--artifacts",
        str(config.artifacts),
        "--config",
        str(path),
        "--code",
        "c" * 43,
    ]


def test_pair_private_config_and_immediate_registration(
    config: WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "worker.json"
    calls = []
    monkeypatch.setattr(
        worker,
        "pair",
        lambda *_: {
            "worker_id": "worker-test",
            "token": "x" * 32,
        },
    )
    monkeypatch.setattr(worker, "_post", lambda *args: calls.append(args[2:]) or {})
    assert cli.main(pair_args(config, path)) == 0
    assert cli.load_config(path).token.get_secret_value() == "x" * 32
    assert path.stat().st_mode & 0o777 == 0o600
    assert calls[0][0] == "/worker/projects"
    assert calls[0][1]["project_id"] == "example"
    assert "x" * 32 not in capsys.readouterr().out
    assert not cli.load_config(path).experimental_execution


def test_experimental_execution_requires_explicit_pair_flag(
    config: WorkerConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "worker.json"
    monkeypatch.setattr(
        worker, "pair", lambda *_: {"worker_id": "worker-test", "token": "x" * 32}
    )
    monkeypatch.setattr(worker, "_post", lambda *_: {})
    assert cli.main([*pair_args(config, path), "--experimental-execution"]) == 0
    assert cli.load_config(path).experimental_execution


def test_pair_records_explicit_github_repository(
    config: WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "worker.json"
    subprocess.run(
        [
            "git",
            "-C",
            str(config.project),
            "remote",
            "add",
            "origin",
            "git@github.com:owner/repo.git",
        ],
        check=True,
    )
    monkeypatch.setattr(
        worker, "pair", lambda *_: {"worker_id": "worker-test", "token": "x" * 32}
    )
    monkeypatch.setattr(worker, "_post", lambda *_: {})
    assert cli.main([*pair_args(config, path), "--github", "owner/repo"]) == 0
    assert json.loads(path.read_text())["github_repository"] == "owner/repo"
    assert cli.load_config(path).github_repository == "owner/repo"
    assert json.loads(capsys.readouterr().out) == {
        "paired": True,
        "registered": True,
        "project_id": config.project_id,
    }


def test_pair_github_origin_mismatch_never_rpc(
    config: WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "worker.json"
    subprocess.run(
        [
            "git",
            "-C",
            str(config.project),
            "remote",
            "add",
            "origin",
            "https://github.com/other/repo.git",
        ],
        check=True,
    )
    monkeypatch.setattr(worker, "pair", lambda *_: pytest.fail("no pairing RPC"))
    monkeypatch.setattr(worker, "_post", lambda *_: pytest.fail("no registration RPC"))
    assert cli.main([*pair_args(config, path), "--github", "owner/repo"]) == 1
    assert not path.exists()


@pytest.mark.parametrize("kind", ["existing", "symlink", "invalid-file", "artifacts"])
def test_pair_invalid_inputs_never_rpc_or_overwrite(
    config: WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    path = tmp_path / "worker.json"
    args = pair_args(config, path)
    if kind == "existing":
        path.write_text("keep")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("keep")
        path.symlink_to(target)
    elif kind == "invalid-file":
        args[args.index("--file") + 1] = ".env"
    else:
        args[args.index("--artifacts") + 1] = str(tmp_path / "missing")
    monkeypatch.setattr(worker, "pair", lambda *_: pytest.fail("no pairing RPC"))
    assert cli.main(args) == 1
    if path.exists():
        assert path.read_text() == "keep"


@pytest.mark.parametrize("kind", ["public", "symlink", "hardlink", "large", "parent"])
def test_load_rejects_unsafe_private_config(
    config: WorkerConfig,
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / "worker.json"
    save(config, path)
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "linked.json"
        link.symlink_to(path)
        path = link
    elif kind == "hardlink":
        os.link(path, tmp_path / "linked.json")
    elif kind == "large":
        path.write_bytes(b"x" * (cli.CONFIG_LIMIT + 1))
    else:
        tmp_path.chmod(0o755)
    with pytest.raises((ValueError, OSError)):
        cli.load_config(path)


def test_worker_summary_drops_all_remote_contents(
    config: WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "worker.json"
    save(config, path)
    monkeypatch.setattr(
        worker,
        "run_once",
        lambda _: {
            "id": "x" * 32,
            "state": "succeeded",
            "lease_token": "x" * 32,
            "result": {"summary": "x" * 32},
        },
    )
    assert cli.main(["worker", "--config", str(path), "--once"]) == 0
    output = capsys.readouterr().out
    assert "x" * 32 not in output
    assert json.loads(output) == {
        "project_id": "example",
        "runs": 1,
        "states": ["succeeded"],
    }


def test_sync_emits_only_locally_derived_summary(
    config: WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "worker.json"
    save(config, path)
    calls: list[WorkerConfig] = []
    monkeypatch.setattr(
        worker,
        "sync",
        lambda selected: (
            calls.append(selected)
            or {
                "revision": "b" * 64,
                "project_id": "untrusted-project",
                "token": "x" * 32,
                "lease_token": "secret-lease",
                "result": {"summary": "private-provider-text"},
                "files": [{"content": "private-source-text"}],
            }
        ),
        raising=False,
    )
    monkeypatch.setattr(worker, "run", lambda *_: pytest.fail("no worker loop"))
    monkeypatch.setattr(
        worker, "run_once", lambda *_: pytest.fail("no claim or execution")
    )
    assert cli.main(["sync", "--config", str(path)]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "synced": True,
        "project_id": config.project_id,
        "revision": "b" * 64,
    }
    assert output.err == ""
    assert calls == [config]


@pytest.mark.parametrize("kind", ["missing", "public", "invalid"])
def test_sync_refuses_bad_config_without_rpc(
    config: WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    path = tmp_path / "worker.json"
    if kind != "missing":
        save(config, path)
        if kind == "public":
            path.chmod(0o644)
        else:
            path.write_text('{"token": "private-invalid-token"}')
    monkeypatch.setattr(
        worker, "sync", lambda *_: pytest.fail("no sync RPC"), raising=False
    )
    assert cli.main(["sync", "--config", str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-invalid-token" not in output.err
    assert "Traceback" not in output.err


def test_sync_remote_failure_redacts_exception(
    config: WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "worker.json"
    save(config, path)
    monkeypatch.setattr(
        worker,
        "sync",
        lambda _: (_ for _ in ()).throw(httpx.ConnectError("private-remote-token")),
        raising=False,
    )
    assert cli.main(["sync", "--config", str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "error": "Command rejected; check private config and prerequisites."
    }


def test_mcp_missing_config_fails_closed_and_redacts_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["mcp", "--config", str(tmp_path / "missing")]) == 1
    assert "Traceback" not in capsys.readouterr().err
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _: (_ for _ in ()).throw(ValueError("secret-token-value")),
    )
    assert cli.main(["worker", "--once"]) == 1
    assert "secret-token-value" not in capsys.readouterr().err


def test_doctor_chatgpt_auth_only_and_no_auth_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def capture(command: list[str], **_: object) -> tuple[int, bytes]:
        calls.append(command)
        if command[-1] == "status":
            return 0, b"Logged in using an API key secret-token-value"
        return 0, b"codex-cli 0.136.0 uv 0.8.0"

    monkeypatch.setattr(worker, "_capture", capture)
    monkeypatch.setattr(
        cli, "load_config", lambda _: pytest.fail("no config requested")
    )
    value = cli.doctor()
    assert value["ok"] is False
    assert value["checks"]["chatgpt_login"] is False
    assert calls == [
        ["codex", "--version"],
        ["codex", "login", "status"],
        ["uv", "--version"],
    ]


def test_pair_failed_registration_retains_private_config(
    config: WorkerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "worker.json"
    monkeypatch.setattr(
        worker,
        "pair",
        lambda *_: {
            "worker_id": "worker-test",
            "token": "x" * 32,
        },
    )
    monkeypatch.setattr(
        worker, "_post", lambda *_: (_ for _ in ()).throw(httpx.ConnectError("secret"))
    )
    assert cli.main(pair_args(config, path)) == 1
    assert cli.load_config(path).worker_id == "worker-test"


def test_pair_revalidates_origin_after_pairing_returns(
    config: WorkerConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "worker.json"
    worker._git(
        config.project, "remote", "add", "origin", "https://github.com/example/project"
    )

    def changed_pair(*_: object) -> dict[str, str]:
        worker._git(
            config.project,
            "remote",
            "set-url",
            "origin",
            "https://github.com/other/project",
        )
        return {"worker_id": "worker-test", "token": "x" * 32}

    monkeypatch.setattr(worker, "pair", changed_pair)
    monkeypatch.setattr(
        worker, "_post", lambda *_: pytest.fail("stale binding must not register")
    )
    assert cli.main([*pair_args(config, path), "--github", "example/project"]) == 1


@pytest.mark.parametrize("root", ["project", "artifacts"])
def test_pair_private_config_cannot_enter_export_or_artifacts(
    config: WorkerConfig,
    monkeypatch: pytest.MonkeyPatch,
    root: str,
) -> None:
    directory = getattr(config, root) / "private-config"
    directory.mkdir(mode=0o700)
    path = directory / "worker.json"
    monkeypatch.setattr(worker, "pair", lambda *_: pytest.fail("no pairing RPC"))
    assert cli.main(pair_args(config, path)) == 1
    assert not path.exists()
