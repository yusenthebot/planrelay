from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from planrelay_bridge import cli, worker
from planrelay_bridge.worker import WorkerConfig


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
