from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from planrelay_bridge import mailbox


@pytest.fixture
def config(tmp_path: Path) -> mailbox.MailboxConfig:
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
    return mailbox.MailboxConfig(
        repository="alice/inbox",
        repository_id=10,
        owner_id=20,
        owner_login="alice",
        project_id="example",
        project=project,
        files=["app.py"],
    )


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch, config: mailbox.MailboxConfig) -> dict:
    data = {
        "repo": {
            "id": 10,
            "private": True,
            "has_issues": True,
            "full_name": "alice/inbox",
            "owner": {"id": 20, "login": "alice", "type": "User"},
        },
        "user": {"id": 20, "login": "alice"},
        "comments": [],
        "contents": {},
        "calls": [],
    }

    def api(
        route: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        missing_ok: bool = False,
    ):
        data["calls"].append((route, method, payload))
        if route == "user":
            return data["user"]
        if route == "repos/alice/inbox":
            return data["repo"]
        if "/git/ref/heads/gpt-connector/task-" in route:
            return None
        if "/contents/" in route:
            path = route.split("/contents/")[1]
            if method == "GET":
                return data["contents"].get(path)
            data["contents"][path] = {
                "type": "file",
                "path": path,
                "encoding": "base64",
                "content": payload["content"],
                "sha": "a" * 40,
            }
            return {"content": {"sha": "a" * 40}}
        if route.endswith("/comments?per_page=100&page=1"):
            return data["comments"]
        if route.endswith("/comments"):
            comment = {"id": 30, "body": payload["body"], "user": {"id": 20}}
            data["comments"].append(comment)
            return comment
        if route.endswith("/issues?state=open&per_page=100&page=1"):
            return [data["issue"]]
        if route.endswith("/issues/1"):
            return data["issue"]
        pytest.fail(f"Unexpected route {route}")

    monkeypatch.setattr(mailbox, "_api", api)
    context = mailbox._snapshot(config)
    body = json.dumps(
        {
            "protocol": "gpt-connector/v1",
            "project_id": "example",
            "revision": context["revision"],
            "request": "Fix greeting",
            "plan": "Edit selected file",
            "approved": True,
        }
    )
    data["issue"] = {
        "number": 1,
        "title": "[GPT Connector] greeting",
        "body": body,
        "state": "open",
        "user": {"id": 20},
    }
    data["contents"]["gpt-connector/context.json"] = {
        "type": "file",
        "path": "gpt-connector/context.json",
        "encoding": "base64",
        "content": base64.b64encode(json.dumps(context).encode()).decode(),
        "sha": "a" * 40,
    }
    return data


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "../bad"},
        {"repository_id": True},
        {"owner_id": "20"},
        {"files": [".env"]},
        {"files": ["app.py", "app.py"]},
        {"extra": "token"},
    ],
)
def test_config_strict(config: mailbox.MailboxConfig, change: dict) -> None:
    with pytest.raises(ValidationError):
        mailbox.MailboxConfig(**{**config.model_dump(), **change})


def test_next_task_is_read_only(config: mailbox.MailboxConfig, remote: dict) -> None:
    task = mailbox.next_task(config, 1)
    assert task["task"]["approved"] is True
    assert len(task["body_sha256"]) == 64
    assert all(method == "GET" for _, method, _ in remote["calls"])


@pytest.mark.parametrize(
    "kind",
    [
        "public",
        "account",
        "repository_id",
        "author",
        "pr",
        "closed",
        "approved",
        "extra",
        "revision",
        "dirty",
    ],
)
def test_task_guards(config: mailbox.MailboxConfig, remote: dict, kind: str) -> None:
    if kind == "public":
        remote["repo"]["private"] = False
    elif kind == "account":
        remote["user"]["id"] = 21
    elif kind == "repository_id":
        remote["repo"]["id"] = 11
    elif kind == "author":
        remote["issue"]["user"]["id"] = 21
    elif kind == "pr":
        remote["issue"]["pull_request"] = {}
    elif kind == "closed":
        remote["issue"]["state"] = "closed"
    elif kind == "dirty":
        (config.project / "untracked.txt").write_text("user data")
    else:
        body = json.loads(remote["issue"]["body"])
        body.update({kind: "0" * 64 if kind == "revision" else "true"})
        remote["issue"]["body"] = json.dumps(body)
    with pytest.raises(ValueError):
        mailbox.next_task(config, 1)


def test_setup_private_config_and_fixed_uploads(
    config: mailbox.MailboxConfig, remote: dict, tmp_path: Path
) -> None:
    path = tmp_path / "private" / "mailbox.json"
    result = mailbox.setup("alice/inbox", config.project, "example", ["app.py"], path)
    assert result["config_path"] == str(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert mailbox.load_config(path).repository_id == 10
    assert {
        route.split("/contents/")[1]
        for route, method, _ in remote["calls"]
        if method == "PUT"
    } == {"README.md", "gpt-connector/context.json"}
    assert str(config.project) not in json.dumps(remote["contents"])
    assert (
        mailbox.setup("alice/inbox", config.project, "example", ["app.py"], path)[
            "existing"
        ]
        is True
    )


def test_setup_refuses_project_config(
    config: mailbox.MailboxConfig, remote: dict
) -> None:
    with pytest.raises(ValueError):
        mailbox.setup(
            "alice/inbox",
            config.project,
            "example",
            ["app.py"],
            config.project / "config.json",
        )
    assert not any(method == "PUT" for _, method, _ in remote["calls"])


def test_result_idempotent_and_no_close(
    config: mailbox.MailboxConfig, remote: dict
) -> None:
    task = mailbox.next_task(config, 1)
    first = mailbox.publish_result(
        config, 1, task["body_sha256"], "Changed greeting", "succeeded"
    )
    again = mailbox.publish_result(
        config, 1, task["body_sha256"], "Changed greeting", "succeeded"
    )
    assert first["comment_id"] == again["comment_id"]
    assert again["existing"] is True
    assert sum(method == "POST" for _, method, _ in remote["calls"]) == 1
    assert remote["issue"]["state"] == "open"


def test_result_rejects_edited_body_and_secrets(
    config: mailbox.MailboxConfig, remote: dict
) -> None:
    task = mailbox.next_task(config, 1)
    with pytest.raises(ValueError):
        mailbox.publish_result(
            config, 1, task["body_sha256"], "sk-" + "a" * 30, "succeeded"
        )
    remote["issue"]["body"] += " "
    with pytest.raises(ValueError):
        mailbox.publish_result(config, 1, task["body_sha256"], "Summary", "succeeded")


def test_context_validation(config: mailbox.MailboxConfig, remote: dict) -> None:
    remote["contents"]["gpt-connector/context.json"]["content"] = base64.b64encode(
        b'{"project_id":"other"}'
    ).decode()
    with pytest.raises(ValueError):
        mailbox.read_context(config)


def test_get_run_does_not_require_clean_worktree(
    config: mailbox.MailboxConfig, remote: dict
) -> None:
    (config.project / "app.py").write_text("changed locally")
    assert mailbox.get_run(config, 1)["task"]["request"] == "Fix greeting"


def test_task_rejects_duplicate_json_fields(
    config: mailbox.MailboxConfig, remote: dict
) -> None:
    remote["issue"]["body"] = remote["issue"]["body"].replace(
        '"approved": true', '"approved": false, "approved": true'
    )
    with pytest.raises(ValueError, match="exact JSON"):
        mailbox.next_task(config, 1)


def test_context_rejects_tampered_excerpt(
    config: mailbox.MailboxConfig, remote: dict
) -> None:
    value = mailbox._snapshot(config)
    value["context"] = value["context"].replace("hello", "other")
    remote["contents"][mailbox.CONTEXT_PATH]["content"] = base64.b64encode(
        json.dumps(value).encode()
    ).decode()
    with pytest.raises(ValueError, match="file hash"):
        mailbox.read_context(config)


def test_result_comments_are_validated(
    config: mailbox.MailboxConfig, remote: dict
) -> None:
    task = mailbox.next_task(config, 1)
    mailbox.publish_result(config, 1, task["body_sha256"], "Done", "succeeded")
    assert mailbox.get_run(config, 1)["results"][0]["summary"] == "Done"
    remote["comments"][0]["body"] += " invalid suffix"
    with pytest.raises(ValueError):
        mailbox.get_run(config, 1)


def test_saturated_comments_prevent_write(
    config: mailbox.MailboxConfig, remote: dict
) -> None:
    task = mailbox.next_task(config, 1)
    remote["comments"] = [{}] * 100
    with pytest.raises(ValueError, match="incomplete"):
        mailbox.publish_result(config, 1, task["body_sha256"], "Done", "succeeded")
    assert not any(method == "POST" for _, method, _ in remote["calls"])


def test_gh_api_safe_argv_and_bounded_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_popen = subprocess.Popen
    seen = []

    def synthetic(command, **kwargs):
        seen.append(command)
        return actual_popen(
            [
                sys.executable,
                "-c",
                "import sys,json; json.load(sys.stdin); print('{}')",
            ],
            **kwargs,
        )

    monkeypatch.setattr(mailbox.shutil, "which", lambda _: "/synthetic/gh")
    monkeypatch.setattr(mailbox.subprocess, "Popen", synthetic)
    assert (
        mailbox._api(
            "repos/alice/inbox/issues/1/comments",
            method="POST",
            payload={"body": "data; $(no shell)"},
        )
        == {}
    )
    assert seen[0] == [
        "/synthetic/gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "POST",
        "repos/alice/inbox/issues/1/comments",
        "--input",
        "-",
    ]
    with pytest.raises(ValueError, match="Unsupported"):
        mailbox._api("https://evil.test/user")


def test_gh_write_output_cap_reports_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_popen = subprocess.Popen
    attempts = []

    def synthetic(command, **kwargs):
        attempts.append(command)
        return actual_popen([sys.executable, "-c", "print('x' * 2000)"], **kwargs)

    monkeypatch.setattr(mailbox.shutil, "which", lambda _: "/synthetic/gh")
    monkeypatch.setattr(mailbox.subprocess, "Popen", synthetic)
    monkeypatch.setattr(mailbox, "API_LIMIT", 100)
    with pytest.raises(ValueError, match="outcome is uncertain"):
        mailbox._api(
            "repos/alice/inbox/issues/1/comments",
            method="POST",
            payload={"body": "data"},
        )
    assert len(attempts) == 1


def test_setup_preserves_existing_readme(
    config: mailbox.MailboxConfig, remote: dict, tmp_path: Path
) -> None:
    remote["contents"]["README.md"] = {
        "type": "file",
        "path": "README.md",
        "encoding": "base64",
        "content": base64.b64encode(b"Existing user documentation").decode(),
        "sha": "a" * 40,
    }
    mailbox.setup(
        "alice/inbox",
        config.project,
        "example",
        ["app.py"],
        tmp_path / "private" / "config.json",
    )
    assert not any(
        route.endswith("/contents/README.md") and method == "PUT"
        for route, method, _ in remote["calls"]
    )


def test_setup_rejects_foreign_context_before_writes(
    config: mailbox.MailboxConfig, remote: dict, tmp_path: Path
) -> None:
    value = mailbox._snapshot(config)
    value["project_id"] = "other-project"
    remote["contents"][mailbox.CONTEXT_PATH]["content"] = base64.b64encode(
        json.dumps(value).encode()
    ).decode()
    with pytest.raises(ValueError, match="project identity"):
        mailbox.setup(
            "alice/inbox",
            config.project,
            "example",
            ["app.py"],
            tmp_path / "private" / "config.json",
        )
    assert not any(method == "PUT" for _, method, _ in remote["calls"])


@pytest.mark.parametrize("flag", ["archived", "disabled"])
def test_inactive_repository_rejected(
    config: mailbox.MailboxConfig, remote: dict, flag: str
) -> None:
    remote["repo"][flag] = True
    with pytest.raises(ValueError):
        mailbox.status(config)


@pytest.fixture
def branches(monkeypatch: pytest.MonkeyPatch, remote: dict) -> dict:
    original = mailbox._api
    data = {"branches": {}, "trees": {}, "commits": {}, "writes": []}

    def api(route, *, method="GET", payload=None, missing_ok=False):
        if (
            "/git/" in route
            or "/contents/" in route
            and ("?ref=" in route or payload and "branch" in payload)
        ):
            if method != "GET":
                data["writes"].append((route, payload))
            if route.endswith("/git/trees"):
                sha = mailbox.digest(mailbox._json(payload))[:40]
                data["trees"][sha] = {
                    item["path"]: item["content"].encode() for item in payload["tree"]
                }
                return {"sha": sha}
            if route.endswith("/git/commits"):
                sha = mailbox.digest(mailbox._json(payload))[:40]
                data["commits"][sha] = data["trees"][payload["tree"]].copy()
                return {"sha": sha}
            if route.endswith("/git/refs"):
                branch = payload["ref"].removeprefix("refs/heads/")
                if branch in data["branches"]:
                    raise ValueError("uncertain concurrent ref creation")
                data["branches"][branch] = payload["sha"]
                return {
                    "ref": payload["ref"],
                    "object": {"type": "commit", "sha": payload["sha"]},
                }
            if "/git/ref/heads/" in route:
                branch = route.split("/git/ref/heads/")[1]
                sha = data["branches"].get(branch)
                return (
                    None
                    if sha is None
                    else {
                        "ref": "refs/heads/" + branch,
                        "object": {"type": "commit", "sha": sha},
                    }
                )
            path, _, ref = route.split("/contents/")[1].partition("?ref=")
            if method == "GET":
                raw = data["commits"][ref].get(path)
                return (
                    None
                    if raw is None
                    else {
                        "type": "file",
                        "path": path,
                        "encoding": "base64",
                        "content": base64.b64encode(raw).decode(),
                        "sha": mailbox.digest(raw)[:40],
                    }
                )
            branch = payload["branch"]
            current = data["commits"][data["branches"][branch]].copy()
            if path in current:
                raise ValueError("existing result must not be overwritten")
            current[path] = base64.b64decode(payload["content"])
            sha = mailbox.digest(mailbox._json(payload))[:40]
            data["commits"][sha] = current
            data["branches"][branch] = sha
            return {
                "content": {"sha": mailbox.digest(current[path])[:40]},
                "commit": {"sha": sha},
            }
        if route.endswith("/issues/2"):
            return {
                **remote["issue"],
                "number": 2,
                "body": remote["issue"]["body"].replace("Fix greeting", "Second task"),
            }
        return original(route, method=method, payload=payload, missing_ok=missing_ok)

    monkeypatch.setattr(mailbox, "_api", api)
    return data


def test_task_branches_atomic_and_distinct(config, remote, branches):
    first = mailbox.start_task(config, 1)
    second = mailbox.start_task(config, 2)
    assert first["branch"] == "gpt-connector/task-1"
    assert second["branch"] == "gpt-connector/task-2"
    assert not first["existing"]
    for sha in branches["branches"].values():
        assert set(branches["commits"][sha]) == {
            mailbox.CONTEXT_PATH,
            "gpt-connector/task.json",
        }
    assert all("/git/" in route for route, _ in branches["writes"])
    assert not any(method == "PUT" for _, method, _ in remote["calls"])


def test_task_branch_resume_ignores_mutable_default(config, remote, branches):
    first = mailbox.start_task(config, 1)
    writes = len(branches["writes"])
    remote["contents"][mailbox.CONTEXT_PATH]["content"] = "invalid"
    again = mailbox.start_task(config, 1)
    assert again["existing"] is True
    assert again["body_sha256"] == first["body_sha256"]
    assert len(branches["writes"]) == writes


@pytest.mark.parametrize("kind", ["issue", "task", "context", "missing", "local"])
def test_task_branch_resume_rejects_drift(config, remote, branches, kind):
    mailbox.start_task(config, 1)
    files = branches["commits"][branches["branches"]["gpt-connector/task-1"]]
    if kind == "issue":
        remote["issue"]["body"] += " "
    elif kind == "local":
        (config.project / "app.py").write_text("local edits")
    elif kind == "missing":
        files.pop("gpt-connector/task.json")
    else:
        path = "gpt-connector/task.json" if kind == "task" else mailbox.CONTEXT_PATH
        value = json.loads(files[path])
        value["project_id"] = "foreign"
        files[path] = json.dumps(value).encode()
    before = len(branches["writes"])
    with pytest.raises(ValueError):
        mailbox.start_task(config, 1)
    assert len(branches["writes"]) == before


def test_branch_result_immutable_and_resume_after_local_edits(config, remote, branches):
    task = mailbox.start_task(config, 1)
    (config.project / "app.py").write_text("user-approved local implementation")
    result = mailbox.publish_result(config, 1, task["body_sha256"], "Done", "succeeded")
    assert result["branch"] == task["branch"]
    writes = len(branches["writes"])
    assert (
        mailbox.publish_result(config, 1, task["body_sha256"], "Done", "succeeded")[
            "existing"
        ]
        is True
    )
    assert len(branches["writes"]) == writes
    assert mailbox.get_run(config, 1)["branch_result"]["summary"] == "Done"
    with pytest.raises(ValueError, match="conflict"):
        mailbox.publish_result(config, 1, task["body_sha256"], "Different", "failed")


def test_branch_result_edited_issue_prevents_writes(config, remote, branches):
    task = mailbox.start_task(config, 1)
    remote["issue"]["body"] += " "
    before = len(branches["writes"])
    with pytest.raises(ValueError):
        mailbox.publish_result(config, 1, task["body_sha256"], "Done", "succeeded")
    assert len(branches["writes"]) == before


def test_branch_uncertain_creation_no_retry(config, remote, branches, monkeypatch):
    original = mailbox._api
    attempts = []

    def fail(route, **kwargs):
        if route.endswith("/git/refs"):
            attempts.append(route)
            raise ValueError("GitHub write outcome is uncertain")
        return original(route, **kwargs)

    monkeypatch.setattr(mailbox, "_api", fail)
    with pytest.raises(ValueError, match="uncertain"):
        mailbox.start_task(config, 1)
    assert len(attempts) == 1


def test_branch_result_uncertain_write_recovers_without_rewrite(
    config, remote, branches, monkeypatch
):
    task = mailbox.start_task(config, 1)
    original = mailbox._api
    attempts = []

    def uncertain(route, **kwargs):
        value = original(route, **kwargs)
        if (
            route.endswith("/contents/gpt-connector/result.json")
            and kwargs.get("method") == "PUT"
        ):
            attempts.append(route)
            raise ValueError("GitHub write outcome is uncertain")
        return value

    monkeypatch.setattr(mailbox, "_api", uncertain)
    with pytest.raises(ValueError, match="uncertain"):
        mailbox.publish_result(config, 1, task["body_sha256"], "Done", "succeeded")
    assert len(attempts) == 1
    assert remote["comments"] == []
    monkeypatch.setattr(mailbox, "_api", original)
    result = mailbox.publish_result(config, 1, task["body_sha256"], "Done", "succeeded")
    assert result["result_existing"] is True
    assert (
        len(
            [
                route
                for route, _ in branches["writes"]
                if route.endswith("/contents/gpt-connector/result.json")
            ]
        )
        == 1
    )


def test_branch_reference_uncertain_success_can_resume(
    config, remote, branches, monkeypatch
):
    original = mailbox._api

    def uncertain(route, **kwargs):
        value = original(route, **kwargs)
        if route.endswith("/git/refs"):
            raise ValueError("GitHub write outcome is uncertain")
        return value

    monkeypatch.setattr(mailbox, "_api", uncertain)
    with pytest.raises(ValueError, match="uncertain"):
        mailbox.start_task(config, 1)
    writes = len(branches["writes"])
    monkeypatch.setattr(mailbox, "_api", original)
    assert mailbox.start_task(config, 1)["existing"] is True
    assert len(branches["writes"]) == writes


def test_branch_pinned_task_numeric_ids_are_strict(config, remote, branches):
    mailbox.start_task(config, 1)
    files = branches["commits"][branches["branches"]["gpt-connector/task-1"]]
    value = json.loads(files[mailbox.TASK_PATH])
    value["issue"] = True
    files[mailbox.TASK_PATH] = json.dumps(value).encode()
    with pytest.raises(ValueError, match="schema"):
        mailbox.branch_task(config, 1)


@pytest.mark.parametrize(
    "route,method,payload",
    [
        (
            "repos/alice/inbox/git/refs",
            "POST",
            {"ref": "refs/heads/main", "sha": "a" * 40},
        ),
        (
            "repos/alice/inbox/git/commits",
            "POST",
            {"tree": "a" * 40, "message": "Task", "parents": ["b" * 40]},
        ),
        (
            "repos/alice/inbox/git/ref/heads/gpt-connector/task-1",
            "PUT",
            {"sha": "a" * 40},
        ),
        (
            "repos/alice/inbox/contents/gpt-connector/task.json",
            "PUT",
            {"content": "data"},
        ),
        (
            "repos/alice/inbox/contents/gpt-connector/result.json",
            "PUT",
            {"content": "data", "message": "Result", "branch": "main"},
        ),
    ],
)
def test_branch_routes_reject_general_git_writes(route, method, payload):
    with pytest.raises(ValueError):
        mailbox._api(route, method=method, payload=payload)
