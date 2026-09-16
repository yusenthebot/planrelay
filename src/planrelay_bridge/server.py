"""Owner-authenticated MCP resource server and narrowly scoped worker API."""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import (
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from .auth import Settings
    from .store import Store

MAX_BODY_BYTES = 1024 * 1024
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Document = Annotated[str, Field(min_length=1, max_length=512 * 1024)]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PairCommand(Command):
    code: Annotated[str, Field(min_length=32, max_length=128)]


class ClaimCommand(Command):
    lease_seconds: Annotated[int, Field(ge=30, le=1200)] = 1200
    project_id: Identifier | None = None


class RelayCommand(Command):
    action: Literal["list_projects", "get_project_context", "get_run"]
    project_id: Identifier | None = None
    run_id: Identifier | None = None


class HeartbeatCommand(Command):
    run_id: Identifier
    lease_token: Identifier


class FinishCommand(HeartbeatCommand):
    state: Literal["succeeded", "blocked", "failed", "cancelled"]
    result: dict[str, Any]


class ProjectFile(Command):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ProjectCommand(Command):
    project_id: Identifier
    revision: Revision
    context: Document
    files: Annotated[list[ProjectFile], Field(min_length=1, max_length=32)]
    git_head: Annotated[str, Field(pattern=r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")]


class OwnerVerifier:
    """Keep injected verifiers from accidentally widening owner authorization."""

    def __init__(self, verifier: TokenVerifier, owner: str) -> None:
        self.verifier = verifier
        self.owner = owner

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await self.verifier.verify_token(token)
        return access if access is not None and access.subject == self.owner else None


class RequestBoundary:
    """Bound streamed bodies before auth/JSON parsing and guard every HTTP route."""

    def __init__(self, app: ASGIApp, security: TransportSecuritySettings) -> None:
        self.app = app
        self.security = TransportSecurityMiddleware(security)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        failure = await self.security.validate_request(request)
        if failure is not None:
            await failure(scope, receive, send)
            return
        length = request.headers.get("content-length")
        if length is not None:
            try:
                oversized = int(length) > MAX_BODY_BYTES or int(length) < 0
            except ValueError:
                await JSONResponse({"error": "Invalid request"}, 400)(
                    scope, receive, send
                )
                return
            if oversized:
                await JSONResponse({"error": "Request too large"}, 413)(
                    scope, receive, send
                )
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES:
                await JSONResponse({"error": "Request too large"}, 413)(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


class PairLimiter:
    """Fixed memory, process-wide pairing attempt budget (including invalid JSON)."""

    def __init__(self) -> None:
        self.attempts: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            return False
        self.attempts.append(now)
        return True


def _security(settings: Settings) -> TransportSecuritySettings:
    public = urlsplit(settings.public_url)
    if public.scheme not in {"http", "https"} or not public.hostname:
        raise ValueError("A valid public URL is required")
    loopback = {"localhost", "127.0.0.1", "::1"}
    if settings.local_token is not None:
        if settings.host not in loopback or public.hostname not in loopback:
            raise ValueError(
                "Local bearer mode requires a loopback host and public URL"
            )
    elif public.scheme != "https":
        raise ValueError("Remote servers require HTTPS")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public.netloc],
        allowed_origins=[f"{public.scheme}://{public.netloc}"],
    )


def make_server(
    settings: Settings,
    store: Store | None = None,
    verifier: TokenVerifier | None = None,
) -> FastMCP:
    """Construct an authenticated remote server; never infer identity from arguments."""
    security = _security(settings)
    if store is None:
        from .store import Store

        store = Store(settings.database)
    if verifier is None:
        from .auth import create_verifier

        verifier = create_verifier(settings)
    server = FastMCP(
        "GPT Connector",
        instructions=(
            "Review project context, submit an approved plan, "
            "and track its local execution."
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path=urlsplit(settings.public_url).path or "/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_BODY_BYTES,
        token_verifier=OwnerVerifier(verifier, settings.owner_subject),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer),
            resource_server_url=AnyHttpUrl(settings.public_url),
            required_scopes=["planrelay"],
            validate_token_resource=True,
        ),
        transport_security=security,
    )

    def owner() -> str:
        access = get_access_token()
        if (
            access is None
            or access.subject is None
            or access.subject != settings.owner_subject
        ):
            raise ToolError("Authentication required")
        return access.subject

    def invoke[T](method: Callable[..., T], *args: Any) -> T:
        try:
            return method(owner(), *args)
        except ValueError:
            raise ToolError("Request rejected") from None

    read = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False
    )
    write = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, openWorldHint=False
    )

    @server.tool(annotations=read)
    def list_projects() -> list[dict[str, Any]]:
        """List only the authenticated owner's registered projects."""
        return invoke(store.list_projects)

    @server.tool(annotations=read)
    def get_project_context(project_id: Identifier) -> dict[str, Any]:
        """Read the latest allowlisted context and revision for a project."""
        return invoke(store.get_project, project_id)

    @server.tool(annotations=write)
    def submit_plan(
        project_id: Identifier,
        request: Document,
        plan: Document,
        revision: Revision,
        idempotency_key: Identifier,
    ) -> dict[str, Any]:
        """Queue an approved plan against an exact registered context revision."""
        return invoke(
            store.submit, project_id, request, plan, revision, idempotency_key
        )

    @server.tool(annotations=read)
    def get_run(run_id: Identifier) -> dict[str, Any]:
        """Get an owner's run state and bounded execution result."""
        return invoke(store.get_job, run_id)

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        )
    )
    def cancel_run(run_id: Identifier) -> dict[str, Any]:
        """Request cancellation of an owner's queued or running execution."""
        return invoke(store.cancel, run_id)

    @server.tool(annotations=write)
    def pair_device() -> dict[str, str]:
        """Create a short-lived one-use pairing secret for your local worker."""
        return {"code": invoke(store.create_pairing)}

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        )
    )
    def revoke_device(worker_id: Identifier) -> dict[str, Any]:
        """Revoke a paired worker and block its unfinished executions."""
        return invoke(store.revoke_worker, worker_id)

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        )
    )
    def rebind_project(project_id: Identifier, worker_id: Identifier) -> dict[str, Any]:
        """Explicitly move a project binding; discard old context and block jobs."""
        return invoke(store.rebind_project, project_id, worker_id)

    limiter = PairLimiter()

    async def command[T: Command](request: Request, model: type[T]) -> T:
        return model.model_validate_json(await request.body())

    def worker(request: Request) -> tuple[str, str]:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 4096:
            raise PermissionError
        try:
            identity = store.worker_identity(token)
        except ValueError:
            raise PermissionError from None
        if identity[0] != settings.owner_subject:
            raise PermissionError
        return identity

    async def dispatch(request: Request) -> Response:
        try:
            action = request.url.path.rsplit("/", 1)[-1]
            if action == "pair":
                if not limiter.allow():
                    return JSONResponse(
                        {"error": "Too many pairing attempts"},
                        429,
                        headers={"Retry-After": "60"},
                    )
                pair = await command(request, PairCommand)
                return JSONResponse(store.redeem_pairing(pair.code))
            owner_id, worker_id = worker(request)
            result: Any
            if action == "projects":
                project = await command(request, ProjectCommand)
                result = store.register_project(
                    owner_id, worker_id, project.model_dump()
                )
            elif action == "claim":
                claim = await command(request, ClaimCommand)
                result = {
                    "job": store.claim(
                        owner_id,
                        worker_id,
                        lease_seconds=claim.lease_seconds,
                        project_id=claim.project_id,
                    )
                }
            elif action == "relay":
                relay = await command(request, RelayCommand)
                result = store.relay_read(
                    owner_id, worker_id, relay.action, relay.project_id, relay.run_id
                )
            elif action == "heartbeat":
                heartbeat = await command(request, HeartbeatCommand)
                result = store.heartbeat(
                    owner_id, worker_id, heartbeat.run_id, heartbeat.lease_token
                )
            else:
                finish = await command(request, FinishCommand)
                result = store.finish(
                    owner_id,
                    worker_id,
                    finish.run_id,
                    finish.lease_token,
                    finish.state,
                    finish.result,
                )
            return JSONResponse(result)
        except PermissionError:
            return JSONResponse({"error": "Authentication required"}, 401)
        except (ValidationError, ValueError, json.JSONDecodeError):
            return JSONResponse({"error": "Request rejected"}, 400)

    for action in ("pair", "projects", "claim", "heartbeat", "finish", "relay"):
        server.custom_route(f"/worker/{action}", methods=["POST"])(dispatch)

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ready"})

    server.custom_route("/health", methods=["GET"])(health)
    return server


def create_app(
    settings: Settings,
    store: Store | None = None,
    verifier: TokenVerifier | None = None,
) -> Starlette:
    """Build the bounded ASGI app for uvicorn or an HTTPS reverse proxy."""
    server = make_server(settings, store=store, verifier=verifier)
    app = server.streamable_http_app()
    app.add_middleware(RequestBoundary, security=_security(settings))
    return app
