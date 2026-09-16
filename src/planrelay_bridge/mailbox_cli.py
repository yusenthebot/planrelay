"""GitHub mailbox commands; reads never execute code, writes require an opt-in."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from .files import read_file, safe_text
from .mailbox_prompt import prompt_pack, web_prompt


def default_path() -> Path:
    return Path(
        os.environ.get(
            "GPT_CONNECTOR_GITHUB_CONFIG",
            str(Path.home() / ".config/gpt-connector/github.json"),
        )
    )


def add_parser(commands: Any) -> None:
    github = commands.add_parser("github", help="Private GitHub mailbox workflow")
    actions = github.add_subparsers(dest="github_action", required=True)
    for name in ("setup", "sync", "status", "next", "start", "result", "prompt"):
        action = actions.add_parser(name)
        action.add_argument("--config", type=Path, default=default_path())
        if name in {"setup", "sync", "start", "result"}:
            action.add_argument(
                "--allow-write",
                action="store_true",
                help="Authorize writes to the specified mailbox",
            )
        if name == "setup":
            action.add_argument("--repository", required=True)
            action.add_argument("--project", type=Path, required=True)
            action.add_argument("--project-id", required=True)
            action.add_argument("--file", action="append", required=True)
        if name in {"next", "start", "result"}:
            action.add_argument("--issue", type=int, required=True)
        if name == "result":
            action.add_argument("--body-sha256", required=True)
            action.add_argument("--summary-file", type=Path, required=True)
            action.add_argument(
                "--state", required=True, choices=["succeeded", "failed", "blocked"]
            )


def run(args: argparse.Namespace) -> dict[str, Any]:
    from . import mailbox

    action = args.github_action
    if action in {"setup", "sync", "start", "result"} and not args.allow_write:
        raise ValueError("Mailbox writes require explicit --allow-write authorization.")
    if action == "setup":
        value = mailbox.setup(
            args.repository,
            args.project,
            args.project_id,
            args.file,
            args.config,
            onboarding=web_prompt(args.repository, args.project_id),
        )
        return {
            **value,
            "web_prompt": web_prompt(args.repository, args.project_id),
            "prompts": prompt_pack(args.repository, args.project_id),
            "web_connection_verified": False,
        }
    config = mailbox.load_config(args.config)
    if action == "sync":
        value = mailbox.sync(config)
        return {
            "synced": True,
            "project_id": config.project_id,
            "revision": value.get("revision"),
        }
    if action == "status":
        return mailbox.status(config)
    if action == "next":
        return mailbox.next_task(config, args.issue)
    if action == "start":
        return mailbox.start_task(config, args.issue)
    if action == "result":
        path = args.summary_file.absolute()
        summary = safe_text(read_file(path.parent, path.name, limit=32_768))
        return mailbox.publish_result(
            config,
            args.issue,
            args.body_sha256,
            summary,
            args.state,
        )
    return {
        "web_prompt": web_prompt(config.repository, config.project_id),
        "prompts": prompt_pack(config.repository, config.project_id),
    }
