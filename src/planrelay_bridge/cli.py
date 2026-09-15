"""Explicit pairing and local runtime entry points; credentials never reach stdout."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

import httpx

from . import worker
from .auth import Settings
from .server import create_app
from .worker import WorkerConfig

CONFIG_LIMIT = 65_536


def default_config_path() -> Path:
    return Path(
        os.environ.get(
            "PLANRELAY_CONFIG", str(Path.home() / ".config/planrelay/worker.json")
        )
    )


def load_config(path: Path) -> WorkerConfig:
    """Load only a bounded regular, owner-private, non-symlink configuration."""
    directory = _config_parent(path)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_nlink != 1
            or info.st_size > CONFIG_LIMIT
        ):
            raise ValueError("Worker configuration must be owner-private and regular.")
        raw = stream.read(CONFIG_LIMIT + 1)
    if len(raw) > CONFIG_LIMIT:
        raise ValueError("Worker configuration exceeds its size limit.")
    return WorkerConfig.model_validate_json(raw)


def _config_parent(path: Path) -> int:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Use an existing owner-private configuration directory.")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _emit(value: dict[str, Any], *, error: bool = False) -> None:
    print(
        json.dumps(value, ensure_ascii=False),
        file=sys.stderr if error else sys.stdout,
    )


def _pair(args: argparse.Namespace) -> None:
    if not args.artifacts.exists() or not args.artifacts.is_dir():
        raise ValueError("Select an existing separate artifacts directory.")
    values = {
        "bridge_url": args.bridge,
        "worker_id": "unpaired",
        "token": "0" * 32,
        "project_id": args.project_id,
        "project": args.project,
        "files": args.file,
        "artifacts": args.artifacts,
        "max_runs": args.max_runs,
        "experimental_execution": args.experimental_execution,
    }
    config = WorkerConfig.model_validate(values)
    baseline = worker.snapshot(config)  # Validate Git root and every file before RPC.
    code = args.code if args.code is not None else getpass.getpass("Pairing code: ")
    if not code:
        raise ValueError("Pairing code is required.")
    target = args.config.resolve()
    if any(
        target == root or root in target.parents
        for root in (config.project, config.artifacts)
    ):
        raise ValueError("Keep private configuration outside project and artifacts.")
    directory = _config_parent(args.config)
    try:
        descriptor = os.open(
            args.config.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
    except BaseException:
        os.close(directory)
        raise
    created = os.fstat(descriptor)
    saved = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            paired = worker.pair(config.bridge_url, code)
            config = WorkerConfig.model_validate(
                {**values, "worker_id": paired["worker_id"], "token": paired["token"]}
            )
            data = config.model_dump(mode="json")
            data["token"] = config.token.get_secret_value()
            json.dump(data, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            saved = True
        with httpx.Client(trust_env=False) as client:
            worker._post(client, config, "/worker/projects", baseline)
    finally:
        try:
            if not saved:
                current = os.stat(
                    args.config.name, dir_fd=directory, follow_symlinks=False
                )
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(args.config.name, dir_fd=directory)
        finally:
            os.close(directory)
    _emit({"paired": True, "registered": True, "project_id": config.project_id})


def _worker_summary(result: Any, config: WorkerConfig) -> dict[str, Any]:
    """Do not print remote/provider payloads, plans, contexts or lease tokens."""
    items = result if isinstance(result, list) else [result] if result else []
    states = {"succeeded", "blocked", "failed", "cancelled", "running", "queued"}
    return {
        "project_id": config.project_id,
        "runs": len(items),
        "states": [
            item.get("state")
            if isinstance(item.get("state"), str) and item.get("state") in states
            else "unknown"
            for item in items
            if isinstance(item, dict)
        ],
    }


def doctor(config_path: Path | None = None) -> dict[str, Any]:
    """Inspect command status, never read Codex authentication files."""
    checks: dict[str, bool] = {}
    for name, command, marker in (
        ("codex", ["codex", "--version"], b"codex-cli"),
        (
            "chatgpt_login",
            ["codex", "login", "status"],
            b"Logged in using ChatGPT",
        ),
        ("uv", ["uv", "--version"], b"uv "),
    ):
        try:
            status, output = worker._capture(command, limit=4096, merge_stderr=True)
            checks[name] = status == 0 and marker in output
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            checks[name] = False
    if config_path is not None:
        try:
            load_config(config_path)
            checks["paired_config"] = True
        except (ValueError, OSError):
            checks["paired_config"] = False
    return {"ok": all(checks.values()), "checks": checks}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="planrelay")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Serve using explicit PLANRELAY settings")
    pairing = commands.add_parser("pair", help="Pair and register explicit excerpts")
    pairing.add_argument("--bridge", required=True)
    pairing.add_argument("--project", type=Path, required=True)
    pairing.add_argument("--project-id", required=True)
    pairing.add_argument("--file", action="append", required=True)
    pairing.add_argument("--artifacts", type=Path, required=True)
    pairing.add_argument("--config", type=Path, default=default_config_path())
    pairing.add_argument("--code", help="Prefer the hidden interactive pairing prompt")
    pairing.add_argument("--max-runs", type=int, default=1)
    pairing.add_argument(
        "--experimental-execution",
        action="store_true",
        help="Opt into experimental macOS execution; descendant cleanup is best-effort",
    )
    local = commands.add_parser("worker", help="Run the paired bounded local worker")
    local.add_argument("--config", type=Path, default=default_config_path())
    local.add_argument("--once", action="store_true")
    proxy = commands.add_parser("mcp", help="Run a paired read-only stdio MCP proxy")
    proxy.add_argument("--config", type=Path, default=default_config_path())
    diagnostic = commands.add_parser("doctor", help="Check local prerequisites")
    diagnostic.add_argument("--config", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            import uvicorn

            settings = Settings.from_environment()
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        elif args.command == "pair":
            _pair(args)
        elif args.command == "worker":
            config = load_config(args.config)
            value = worker.run_once(config) if args.once else worker.run(config)
            _emit(_worker_summary(value, config))
        elif args.command == "mcp":
            from .proxy import create_proxy

            create_proxy(load_config(args.config)).run(transport="stdio")
        else:
            value = doctor(args.config)
            _emit(value)
            return 0 if value["ok"] else 1
    except KeyboardInterrupt:
        _emit(
            {"error": "Interrupted; best-effort execution cleanup requested."},
            error=True,
        )
        return 130
    except (
        ValueError,
        OSError,
        KeyError,
        httpx.HTTPError,
        subprocess.SubprocessError,
        EOFError,
        TimeoutError,
    ):
        _emit(
            {"error": "Command rejected; check private config and prerequisites."},
            error=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
