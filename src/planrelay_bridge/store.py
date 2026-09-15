"""Private SQLite queue. Expired jobs are blocked, never blindly replayed."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}|\bAKIA[A-Z0-9]{16}\b"
)


class RelayError(ValueError):
    """Safe error messages without user contents or tokens."""


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def encoded(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise RelayError("Payload requires bounded finite JSON values.") from error


def safe_text(value: str, limit: int, *, nonempty: bool = True) -> str:
    try:
        if not isinstance(value, str) or len(value.encode()) > limit:
            raise ValueError("size or type")
        if "\x00" in value or (nonempty and not value.strip()) or SECRET.search(value):
            raise ValueError("unsafe content")
    except (ValueError, UnicodeError) as error:
        raise RelayError(
            "Content is empty, oversized, invalid text, or contains a known credential."
        ) from error
    return value


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise RelayError("Invalid project, task, or device identifier.")
    return value


class FileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    context: str = Field(min_length=1, max_length=524288)
    git_head: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    files: list[FileRecord] = Field(min_length=1, max_length=32)


class Store:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        with self.connection() as database:
            database.executescript(
                "CREATE TABLE IF NOT EXISTS pairs "
                "(hash TEXT PRIMARY KEY, owner TEXT, expires REAL);"
                "CREATE TABLE IF NOT EXISTS workers "
                "(id TEXT PRIMARY KEY, owner TEXT, "
                "token_hash TEXT UNIQUE, expires REAL);"
                "CREATE TABLE IF NOT EXISTS projects (owner TEXT, id TEXT, "
                "worker TEXT, payload TEXT, PRIMARY KEY(owner,id));"
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, "
                "project TEXT, request TEXT, plan TEXT, revision TEXT, idem TEXT, "
                "signature TEXT, state TEXT, worker TEXT, lease_hash TEXT, "
                "deadline REAL, result TEXT, created REAL, UNIQUE(owner,idem));"
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        database = sqlite3.connect(self.path, timeout=5)
        database.row_factory = sqlite3.Row
        try:
            with database:
                yield database
        except sqlite3.Error as error:
            raise RelayError(
                "Queue operation failed; check database availability."
            ) from error
        finally:
            database.close()

    def create_pairing(self, owner: str) -> str:
        code = secrets.token_urlsafe(32)
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute("DELETE FROM pairs WHERE expires <= ?", (self.clock(),))
            count = database.execute(
                "SELECT count(*) FROM pairs WHERE owner=?", (owner,)
            ).fetchone()[0]
            if count >= 8:
                raise RelayError("Too many active pairing codes; wait for expiry.")
            database.execute(
                "INSERT INTO pairs VALUES (?,?,?)",
                (hashed(code), owner, self.clock() + 300),
            )
        return code

    def redeem_pairing(self, code: str) -> dict[str, str]:
        identifier(code)
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            pair = database.execute(
                "SELECT * FROM pairs WHERE hash=? AND expires>?",
                (hashed(code), self.clock()),
            ).fetchone()
            if pair is None:
                raise RelayError("Pairing code is invalid, expired, or already used.")
            count = database.execute(
                "SELECT count(*) FROM workers WHERE owner=? AND expires>?",
                (pair["owner"], self.clock()),
            ).fetchone()[0]
            if count >= 8:
                raise RelayError("Device limit reached; revoke an old device first.")
            database.execute("DELETE FROM pairs WHERE hash=?", (hashed(code),))
            worker, token = secrets.token_hex(16), secrets.token_urlsafe(32)
            database.execute(
                "INSERT INTO workers VALUES (?,?,?,?)",
                (worker, pair["owner"], hashed(token), self.clock() + 30 * 86400),
            )
        return {"worker_id": worker, "token": token}

    def worker_identity(self, token: str) -> tuple[str, str]:
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise RelayError("Device authentication failed.")
        with self.connection() as database:
            worker = database.execute(
                "SELECT * FROM workers WHERE token_hash=? AND expires>?",
                (hashed(token), self.clock()),
            ).fetchone()
            if worker is None:
                raise RelayError("Device authentication failed.")
            return worker["owner"], worker["id"]

    def _worker(self, database: sqlite3.Connection, owner: str, worker: str) -> None:
        if (
            database.execute(
                "SELECT id FROM workers WHERE id=? AND owner=? AND expires>?",
                (worker, owner, self.clock()),
            ).fetchone()
            is None
        ):
            raise RelayError("Device authentication failed.")

    def revoke_worker(self, owner: str, worker_id: str) -> dict[str, Any]:
        identifier(worker_id)
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT id FROM workers WHERE owner=? AND id=?", (owner, worker_id)
            ).fetchone()
            if row is None:
                raise RelayError("Device not found or not accessible.")
            database.execute(
                "UPDATE workers SET expires=0 WHERE owner=? AND id=?",
                (owner, worker_id),
            )
            database.execute(
                "UPDATE jobs SET state='blocked',result=? "
                "WHERE owner=? AND worker=? AND state='running'",
                (
                    encoded({"summary": "Device revoked; automatic replay disabled."}),
                    owner,
                    worker_id,
                ),
            )
        return {"worker_id": worker_id, "revoked": True}

    def relay_read(
        self,
        owner: str,
        worker_id: str,
        action: str,
        project_id: str | None = None,
        run_id: str | None = None,
    ) -> Any:
        with self.connection() as database:
            self._worker(database, owner, worker_id)
            projects = {
                row["id"]
                for row in database.execute(
                    "SELECT id FROM projects WHERE owner=? AND worker=?",
                    (owner, worker_id),
                )
            }
        if action == "list_projects":
            return [
                item
                for item in self.list_projects(owner)
                if item["project_id"] in projects
            ]
        if action == "get_project_context" and project_id in projects:
            return self.get_project(owner, str(project_id))
        if action == "get_run" and run_id is not None:
            job = self.get_job(owner, run_id)
            if job["project_id"] in projects:
                return job
        raise RelayError("Read operation not accessible to this device.")

    def rebind_project(
        self, owner: str, project_id: str, worker_id: str
    ) -> dict[str, Any]:
        """Explicit owner recovery only; invalidate pending jobs and old context."""
        identifier(project_id)
        identifier(worker_id)
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            self._worker(database, owner, worker_id)
            project = database.execute(
                "SELECT worker FROM projects WHERE owner=? AND id=?",
                (owner, project_id),
            ).fetchone()
            if project is None:
                raise RelayError("Project not found or not accessible.")
            if project["worker"] == worker_id:
                raise RelayError("Project is already bound to this device.")
            database.execute(
                "UPDATE jobs SET state='blocked',result=? WHERE owner=? "
                "AND project=? AND state IN ('queued','running')",
                (
                    encoded(
                        {
                            "summary": (
                                "Project binding changed; "
                                "resubmit after refreshing context."
                            )
                        }
                    ),
                    owner,
                    project_id,
                ),
            )
            database.execute(
                "DELETE FROM projects WHERE owner=? AND id=?", (owner, project_id)
            )
            # Bind immediately, but expose no old-device source to the new worker.
            empty: dict[str, Any] = {
                "project_id": project_id,
                "revision": "unregistered",
                "context": "",
                "files": [],
                "git_head": None,
            }
            database.execute(
                "INSERT INTO projects VALUES (?,?,?,?)",
                (owner, project_id, worker_id, encoded(empty)),
            )
        return {
            "project_id": project_id,
            "worker_id": worker_id,
            "requires_registration": True,
        }

    def register_project(
        self, owner: str, worker_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            snapshot = Snapshot.model_validate(payload)
        except ValidationError as error:
            raise RelayError("Invalid project snapshot.") from error
        safe_text(snapshot.context, 524288)
        seen: set[str] = set()
        for record in snapshot.files:
            safe_text(record.path, 1024)
            parts = record.path.split("/")
            if (
                record.path.startswith("/")
                or "\\" in record.path
                or any(
                    part in {"", ".", "..", ".git", ".venv", "node_modules"}
                    or part.lower().startswith((".env", "id_rsa", "id_ed25519"))
                    or part.lower().endswith((".pem", ".key", ".p12", ".pfx"))
                    or part.lower() in {"credentials.json", "secrets.json"}
                    for part in parts
                )
            ):
                raise RelayError("Invalid or sensitive source path.")
            if record.path in seen:
                raise RelayError("Snapshot paths must be unique.")
            seen.add(record.path)
        data = snapshot.model_dump()
        if (
            hashed(encoded({"git_head": data["git_head"], "files": data["files"]}))
            != snapshot.revision
        ):
            raise RelayError("Snapshot revision does not match its baseline.")
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            self._worker(database, owner, worker_id)
            old = database.execute(
                "SELECT worker FROM projects WHERE owner=? AND id=?",
                (owner, snapshot.project_id),
            ).fetchone()
            if old is not None and old["worker"] != worker_id:
                raise RelayError("Project is already bound to another device.")
            if (
                old is None
                and database.execute(
                    "SELECT count(*) FROM projects WHERE owner=?", (owner,)
                ).fetchone()[0]
                >= 32
            ):
                raise RelayError("Project limit reached.")
            database.execute(
                "INSERT INTO projects VALUES (?,?,?,?) ON CONFLICT(owner,id) "
                "DO UPDATE SET payload=excluded.payload "
                "WHERE projects.worker=excluded.worker",
                (owner, snapshot.project_id, worker_id, encoded(data)),
            )
        return data

    def list_projects(self, owner: str) -> list[dict[str, Any]]:
        with self.connection() as database:
            return [
                {
                    "project_id": row["id"],
                    "revision": json.loads(row["payload"])["revision"],
                }
                for row in database.execute(
                    "SELECT id,payload FROM projects WHERE owner=? "
                    "ORDER BY id LIMIT 32",
                    (owner,),
                )
            ]

    def get_project(self, owner: str, project_id: str) -> dict[str, Any]:
        identifier(project_id)
        with self.connection() as database:
            row = database.execute(
                "SELECT payload FROM projects WHERE owner=? AND id=?",
                (owner, project_id),
            ).fetchone()
            if row is None:
                raise RelayError("Project not found or not accessible.")
            return json.loads(row["payload"])  # type: ignore[no-any-return]

    def submit(
        self,
        owner: str,
        project_id: str,
        request: str,
        plan: str,
        revision: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        identifier(project_id)
        identifier(idempotency_key)
        safe_text(request, 32768)
        safe_text(plan, 524288)
        signature = hashed(encoded([project_id, request, plan, revision]))
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            prior = database.execute(
                "SELECT * FROM jobs WHERE owner=? AND idem=?", (owner, idempotency_key)
            ).fetchone()
            if prior is not None:
                if prior["signature"] != signature:
                    raise RelayError(
                        "Idempotency key already used for a different task."
                    )
                return self.public_job(prior)
            project = database.execute(
                "SELECT payload FROM projects WHERE owner=? AND id=?",
                (owner, project_id),
            ).fetchone()
            if (
                project is None
                or json.loads(project["payload"])["revision"] != revision
            ):
                raise RelayError(
                    "Project baseline is missing or stale; refresh context."
                )
            if (
                database.execute(
                    "SELECT count(*) FROM jobs WHERE owner=? "
                    "AND state IN ('queued','running')",
                    (owner,),
                ).fetchone()[0]
                >= 32
            ):
                raise RelayError("Pending task limit reached.")
            job_id = secrets.token_hex(16)
            database.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    owner,
                    project_id,
                    request,
                    plan,
                    revision,
                    idempotency_key,
                    signature,
                    "queued",
                    None,
                    None,
                    None,
                    None,
                    self.clock(),
                ),
            )
            row = database.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self.public_job(row)

    @staticmethod
    def public_job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project"],
            "request": row["request"],
            "plan": row["plan"],
            "revision": row["revision"],
            "state": row["state"],
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def _expire(self, database: sqlite3.Connection) -> None:
        database.execute(
            "UPDATE jobs SET state='blocked', result=? "
            "WHERE state='running' AND deadline<=?",
            (
                encoded(
                    {"summary": "Worker lease expired; automatic replay is disabled."}
                ),
                self.clock(),
            ),
        )

    def get_job(self, owner: str, run_id: str) -> dict[str, Any]:
        identifier(run_id)
        with self.connection() as database:
            self._expire(database)
            row = database.execute(
                "SELECT * FROM jobs WHERE id=? AND owner=?", (run_id, owner)
            ).fetchone()
            if row is None:
                raise RelayError("Task not found or not accessible.")
            return self.public_job(row)

    def claim(
        self,
        owner: str,
        worker_id: str,
        lease_seconds: int = 1200,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        if project_id is not None:
            identifier(project_id)
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600:
            raise RelayError("Lease must be 30–3600 seconds.")
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            self._worker(database, owner, worker_id)
            self._expire(database)
            if database.execute(
                "SELECT id FROM jobs WHERE owner=? AND worker=? AND state='running'",
                (owner, worker_id),
            ).fetchone():
                return None
            row = database.execute(
                "SELECT jobs.* FROM jobs JOIN projects "
                "ON jobs.owner=projects.owner AND jobs.project=projects.id "
                "WHERE jobs.owner=? AND projects.worker=? AND jobs.state='queued' "
                "AND (? IS NULL OR jobs.project=?) "
                "ORDER BY jobs.created,jobs.id LIMIT 1",
                (owner, worker_id, project_id, project_id),
            ).fetchone()
            if row is None:
                return None
            lease = secrets.token_urlsafe(32)
            database.execute(
                "UPDATE jobs SET state='running',worker=?,lease_hash=?,deadline=? "
                "WHERE id=?",
                (worker_id, hashed(lease), self.clock() + lease_seconds, row["id"]),
            )
            row = database.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],)
            ).fetchone()
            return {**self.public_job(row), "lease_token": lease}

    def _leased(
        self,
        database: sqlite3.Connection,
        owner: str,
        worker: str,
        run_id: str,
        lease: str,
    ) -> sqlite3.Row:
        self._worker(database, owner, worker)
        identifier(run_id)
        identifier(lease)
        self._expire(database)
        row = database.execute(
            "SELECT * FROM jobs WHERE id=? AND owner=? AND worker=? AND lease_hash=?",
            (run_id, owner, worker, hashed(lease)),
        ).fetchone()
        if row is None:
            raise RelayError("Task lease is invalid or not accessible.")
        return row  # type: ignore[no-any-return]

    def heartbeat(
        self, owner: str, worker_id: str, run_id: str, lease: str
    ) -> dict[str, Any]:
        with self.connection() as database:
            row = self._leased(database, owner, worker_id, run_id, lease)
            if row["state"] == "running":
                database.execute(
                    "UPDATE jobs SET deadline=? WHERE id=?", (self.clock() + 60, run_id)
                )
            return self.public_job(row)

    def finish(
        self,
        owner: str,
        worker_id: str,
        run_id: str,
        lease: str,
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in {
            "succeeded",
            "blocked",
            "failed",
            "cancelled",
        } or not isinstance(result, dict):
            raise RelayError("Invalid completion state or result.")
        data = safe_text(encoded(result), 524288)
        with self.connection() as database:
            row = self._leased(database, owner, worker_id, run_id, lease)
            if row["state"] == status and row["result"] == data:
                return self.public_job(row)
            if row["state"] != "running":
                raise RelayError("Task is no longer running; result was not accepted.")
            database.execute(
                "UPDATE jobs SET state=?,result=? WHERE id=?", (status, data, run_id)
            )
            row = database.execute(
                "SELECT * FROM jobs WHERE id=?", (run_id,)
            ).fetchone()
            return self.public_job(row)

    def cancel(self, owner: str, run_id: str) -> dict[str, Any]:
        identifier(run_id)
        with self.connection() as database:
            row = database.execute(
                "SELECT * FROM jobs WHERE id=? AND owner=?", (run_id, owner)
            ).fetchone()
            if row is None:
                raise RelayError("Task not found or not accessible.")
            database.execute(
                "UPDATE jobs SET state='cancelled' WHERE id=? "
                "AND state IN ('queued','running')",
                (run_id,),
            )
            row = database.execute(
                "SELECT * FROM jobs WHERE id=?", (run_id,)
            ).fetchone()
            return self.public_job(row)
