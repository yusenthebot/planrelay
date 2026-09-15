"""Authenticated HTTP surface tests, including a real MCP SDK session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from pydantic import SecretStr
from starlette.testclient import TestClient

from planrelay_bridge.server import MAX_BODY_BYTES, create_app, make_server
from planrelay_bridge.store import Store, encoded, hashed


class Verifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "web-secret":
            return None
        return AccessToken(
            token=token,
            client_id="chatgpt",
            scopes=["planrelay"],
            resource="https://relay.example/mcp",
            subject="owner",
        )


class BadClaimsVerifier:
    def __init__(self, field: str, value: Any) -> None:
        self.field, self.value = field, value

    async def verify_token(self, token: str) -> AccessToken | None:
        claims = {
            "token": token,
            "client_id": "chatgpt",
            "scopes": ["planrelay"],
            "resource": "https://relay.example/mcp",
            "subject": "owner",
        }
        claims[self.field] = self.value
        return AccessToken.model_validate(claims)


class StubStore:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.paired = False

    def worker_identity(self, token: str) -> tuple[str, str]:
        if token != "worker-secret":
            raise ValueError("secret-sensitive error")
        return "owner", "worker"

    def redeem_pairing(self, code: str) -> dict[str, str]:
        if code != "x" * 43 or self.paired:
            raise ValueError("secret-sensitive error")
        self.paired = True
        return {"worker_id": "worker", "token": "worker-secret"}

    def create_pairing(self, owner: str) -> str:
        self.calls.append(("pair", owner))
        return "x" * 43

    def list_projects(self, owner: str) -> list[dict[str, str]]:
        self.calls.append(("list", owner))
        return [{"id": "project", "name": "Demo"}]

    def get_project(self, owner: str, project_id: str) -> dict[str, str]:
        self.calls.append(("context", owner, project_id))
        return {"id": project_id, "context": "README"}

    def submit(self, *args: Any) -> dict[str, str]:
        self.calls.append(("submit", *args))
        return {"id": "run", "state": "queued"}

    def get_job(self, owner: str, run_id: str) -> dict[str, str]:
        self.calls.append(("get", owner, run_id))
        return {"id": run_id, "state": "queued"}

    def cancel(self, owner: str, run_id: str) -> dict[str, str]:
        self.calls.append(("cancel", owner, run_id))
        return {"id": run_id, "state": "cancelled"}

    def register_project(self, *args: Any) -> dict[str, str]:
        self.calls.append(("register", *args))
        return {"id": "project"}

    def claim(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("claim", *args, kwargs))

    def heartbeat(self, *args: Any) -> dict[str, str]:
        self.calls.append(("heartbeat", *args))
        return {"state": "running"}

    def finish(self, *args: Any) -> dict[str, str]:
        self.calls.append(("finish", *args))
        return {"state": "succeeded"}

    def revoke_worker(self, owner: str, worker_id: str) -> dict[str, str]:
        self.calls.append(("revoke", owner, worker_id))
        return {"worker_id": worker_id, "state": "revoked"}

    def relay_read(self, *args: Any) -> dict[str, str]:
        self.calls.append(("relay", *args))
        return {"state": "queued"}


@pytest.fixture
def settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        public_url="https://relay.example/mcp",
        issuer="https://identity.example/",
        jwks_url="https://identity.example/jwks",
        owner_subject="owner",
        local_token=None,
        database=tmp_path / "relay.db",
        host="127.0.0.1",
        port=8123,
    )


@pytest.fixture
def store() -> StubStore:
    return StubStore()


@pytest.fixture
def client(settings: SimpleNamespace, store: StubStore):
    with TestClient(
        create_app(settings, store=store, verifier=Verifier()),
        base_url="https://relay.example",
    ) as client:
        yield client


@pytest.mark.parametrize("token", [None, "wrong-secret", "worker-secret"])
def test_mcp_requires_web_bearer(client: TestClient, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post("/mcp", json={}, headers=headers)
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_metadata_and_health(client: TestClient) -> None:
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == "https://relay.example/mcp"
    assert metadata["authorization_servers"] == ["https://identity.example/"]
    assert metadata["scopes_supported"] == ["planrelay"]
    assert client.get("/health").json() == {"status": "ready"}


@pytest.mark.parametrize(
    ("field", "value", "status"),
    [
        ("subject", "attacker", 401),
        ("resource", "https://other.example/mcp", 401),
        ("scopes", [], 403),
    ],
)
def test_owner_audience_and_scope_are_required(
    settings: SimpleNamespace, field: str, value: Any, status: int
) -> None:
    with TestClient(
        create_app(
            settings, store=StubStore(), verifier=BadClaimsVerifier(field, value)
        ),
        base_url="https://relay.example",
    ) as client:
        response = client.post(
            "/mcp", json={}, headers={"Authorization": "Bearer token"}
        )
    assert response.status_code == status


@pytest.mark.parametrize("token", [None, "wrong-secret", "web-secret"])
def test_worker_requires_separate_secret(client: TestClient, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post("/worker/claim", json={}, headers=headers)
    assert response.status_code == 401
    assert "secret" not in response.text


def test_pairing_is_single_use_and_does_not_leak_errors(client: TestClient) -> None:
    response = client.post("/worker/pair", json={"code": "x" * 43})
    assert response.json()["token"] == "worker-secret"
    response = client.post("/worker/pair", json={"code": "x" * 43})
    assert response.status_code == 400
    assert "secret-sensitive" not in response.text


def test_pairing_attempts_are_rate_limited(client: TestClient) -> None:
    for _ in range(10):
        client.post("/worker/pair", json={"code": "z" * 43})
    assert client.post("/worker/pair", json={"code": "z" * 43}).status_code == 429


def test_worker_schema_and_body_limits(client: TestClient) -> None:
    headers = {"Authorization": "Bearer worker-secret"}
    assert (
        client.post(
            "/worker/claim", json={"owner": "attacker"}, headers=headers
        ).status_code
        == 400
    )

    assert (
        client.post(
            "/worker/claim", json={"lease_seconds": 0}, headers=headers
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/worker/projects", content=b"x" * (MAX_BODY_BYTES + 1), headers=headers
        ).status_code
        == 413
    )
    assert client.post("/worker/claim", content="{", headers=headers).status_code == 400


def test_streamed_body_is_bounded(client: TestClient) -> None:
    def chunks():
        yield b"x" * MAX_BODY_BYTES
        yield b"x"

    assert client.post("/worker/pair", content=chunks()).status_code == 413


def test_worker_routes_bind_identity(client: TestClient, store: StubStore) -> None:
    headers = {"Authorization": "Bearer worker-secret"}
    assert (
        client.post(
            "/worker/projects",
            json={
                "project_id": "project",
                "revision": "a" * 64,
                "context": "README",
                "files": [{"path": "README.md", "sha256": "b" * 64}],
                "git_head": "c" * 40,
            },
            headers=headers,
        ).status_code
        == 200
    )
    assert client.post(
        "/worker/claim", json={"lease_seconds": 60}, headers=headers
    ).json() == {"job": None}
    assert (
        client.post(
            "/worker/heartbeat",
            json={"run_id": "run", "lease_token": "lease"},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/worker/finish",
            json={
                "run_id": "run",
                "lease_token": "lease",
                "state": "succeeded",
                "result": {},
            },
            headers=headers,
        ).status_code
        == 200
    )
    assert all(call[1:3] == ("owner", "worker") for call in store.calls)


def test_claim_passes_configured_project(client: TestClient, store: StubStore) -> None:
    response = client.post(
        "/worker/claim",
        json={"project_id": "project", "lease_seconds": 60},
        headers={"Authorization": "Bearer worker-secret"},
    )
    assert response.status_code == 200
    assert store.calls[-1] == (
        "claim",
        "owner",
        "worker",
        {"lease_seconds": 60, "project_id": "project"},
    )
    assert (
        client.post(
            "/worker/claim",
            json={"project_id": "../escape"},
            headers={"Authorization": "Bearer worker-secret"},
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "list_projects"},
        {"action": "get_project_context", "project_id": "project"},
        {"action": "get_run", "run_id": "run"},
    ],
)
def test_worker_relay_binds_identity(
    client: TestClient, store: StubStore, payload: dict[str, str]
) -> None:
    response = client.post(
        "/worker/relay", json=payload, headers={"Authorization": "Bearer worker-secret"}
    )
    assert response.status_code == 200
    assert store.calls[-1] == (
        "relay",
        "owner",
        "worker",
        payload["action"],
        payload.get("project_id"),
        payload.get("run_id"),
    )


@pytest.mark.parametrize("token", [None, "web-secret", "wrong-secret"])
def test_worker_relay_requires_device_auth(
    client: TestClient, token: str | None
) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    assert (
        client.post(
            "/worker/relay", json={"action": "list_projects"}, headers=headers
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "submit_plan"},
        {"action": "get_run", "run_id": "../escape"},
        {"action": "list_projects", "owner": "attacker"},
    ],
)
def test_worker_relay_only_accepts_scoped_reads(
    client: TestClient, payload: dict[str, str]
) -> None:
    assert (
        client.post(
            "/worker/relay",
            json=payload,
            headers={"Authorization": "Bearer worker-secret"},
        ).status_code
        == 400
    )


def test_local_static_mode_cannot_bind_public_host(settings: SimpleNamespace) -> None:
    settings.local_token = SecretStr("x" * 32)
    settings.host = "0.0.0.0"
    with pytest.raises(ValueError, match="loopback"):
        make_server(settings, store=StubStore(), verifier=Verifier())


def test_dns_rebinding_rejected(client: TestClient) -> None:
    headers = {
        "Authorization": "Bearer web-secret",
        "Host": "attacker.example",
        "Accept": "application/json, text/event-stream",
    }
    assert client.post("/mcp", json={}, headers=headers).status_code == 421


def test_real_sdk_tool_roundtrip(settings: SimpleNamespace, store: StubStore) -> None:
    app = create_app(settings, store=store, verifier=Verifier())

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://relay.example",
                headers={"Authorization": "Bearer web-secret"},
            ) as http_client:
                async with streamable_http_client(
                    "https://relay.example/mcp", http_client=http_client
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = (await session.list_tools()).tools
                        assert {tool.name for tool in tools} == {
                            "list_projects",
                            "get_project_context",
                            "submit_plan",
                            "get_run",
                            "cancel_run",
                            "pair_device",
                            "revoke_device",
                            "rebind_project",
                        }
                        assert (
                            next(
                                tool for tool in tools if tool.name == "submit_plan"
                            ).annotations.readOnlyHint
                            is False
                        )
                        assert (
                            next(
                                tool for tool in tools if tool.name == "cancel_run"
                            ).annotations.destructiveHint
                            is True
                        )
                        result = await session.call_tool("list_projects", {})
                        assert not result.isError
                        assert "Demo" in json.dumps(result.model_dump())
                        result = await session.call_tool(
                            "submit_plan",
                            {
                                "project_id": "project",
                                "request": "Build",
                                "plan": "Test then implement",
                                "revision": "a" * 64,
                                "idempotency_key": "key",
                            },
                        )
                        assert not result.isError
                        await session.call_tool(
                            "get_project_context", {"project_id": "project"}
                        )
                        await session.call_tool("get_run", {"run_id": "run"})
                        await session.call_tool("cancel_run", {"run_id": "run"})
                        await session.call_tool("pair_device", {})
                        result = await session.call_tool(
                            "revoke_device", {"worker_id": "worker"}
                        )
                        assert not result.isError
                        assert (
                            next(
                                tool for tool in tools if tool.name == "revoke_device"
                            ).annotations.destructiveHint
                            is True
                        )

    asyncio.run(exercise())
    assert all(call[1] == "owner" for call in store.calls)


def test_real_store_http_worker_and_mcp_lifecycle(settings: SimpleNamespace) -> None:
    store = Store(settings.database)
    baseline = {
        "git_head": "c" * 40,
        "files": [{"path": "README.md", "sha256": "b" * 64}],
    }
    revision = hashed(encoded(baseline))
    app = create_app(settings, store=store, verifier=Verifier())

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://relay.example"
            ) as http_client:
                pairing = await http_client.post(
                    "/worker/pair", json={"code": store.create_pairing("owner")}
                )
                worker_headers = {"Authorization": f"Bearer {pairing.json()['token']}"}
                registered = await http_client.post(
                    "/worker/projects",
                    json={
                        "project_id": "project",
                        "revision": revision,
                        "context": "README context",
                        **baseline,
                    },
                    headers=worker_headers,
                )
                assert registered.status_code == 200
                readable = await http_client.post(
                    "/worker/relay",
                    json={"action": "get_project_context", "project_id": "project"},
                    headers=worker_headers,
                )
                assert readable.status_code == 200
                assert readable.json()["revision"] == revision
                http_client.headers["Authorization"] = "Bearer web-secret"
                async with streamable_http_client(
                    "https://relay.example/mcp", http_client=http_client
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "submit_plan",
                            {
                                "project_id": "project",
                                "request": "Build",
                                "plan": "Test then implement",
                                "revision": revision,
                                "idempotency_key": "one",
                            },
                        )
                        assert not result.isError
                        assert result.structuredContent is not None
                        run_id = result.structuredContent["id"]
                        claimed = await http_client.post(
                            "/worker/claim",
                            json={"lease_seconds": 60, "project_id": "project"},
                            headers=worker_headers,
                        )
                        job = claimed.json()["job"]
                        assert job["id"] == run_id
                        finished = await http_client.post(
                            "/worker/finish",
                            json={
                                "run_id": run_id,
                                "lease_token": job["lease_token"],
                                "state": "succeeded",
                                "result": {"summary": "Verified"},
                            },
                            headers=worker_headers,
                        )
                        assert finished.status_code == 200
                        result = await session.call_tool("get_run", {"run_id": run_id})
                        assert result.structuredContent["state"] == "succeeded"
                        readable = await http_client.post(
                            "/worker/relay",
                            json={"action": "get_run", "run_id": run_id},
                            headers=worker_headers,
                        )
                        assert readable.json()["state"] == "succeeded"
                        revoked = await session.call_tool(
                            "revoke_device", {"worker_id": pairing.json()["worker_id"]}
                        )
                        assert not revoked.isError
                        assert (
                            await http_client.post(
                                "/worker/relay",
                                json={"action": "list_projects"},
                                headers=worker_headers,
                            )
                        ).status_code == 401

    asyncio.run(exercise())
