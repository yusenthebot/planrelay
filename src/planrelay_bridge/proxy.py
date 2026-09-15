"""Paired stdio MCP adapter exposing only three project-scoped read tools."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import worker
from .server import Identifier
from .worker import WorkerConfig


def create_proxy(config: WorkerConfig) -> FastMCP:
    server = FastMCP(
        "PlanRelay local read-only proxy",
        instructions="Read only the paired project's context and run status.",
    )
    read = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False
    )

    def relay(action: str, *, run_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": action,
            "project_id": config.project_id,
        }
        if run_id is not None:
            payload["run_id"] = run_id
        try:
            with httpx.Client(trust_env=False) as client:
                return worker._post(client, config, "/worker/relay", payload)
        except (ValueError, OSError, httpx.HTTPError):
            raise ToolError("Paired read request rejected.") from None

    def scoped(value: dict[str, Any]) -> dict[str, Any]:
        if value.get("project_id") != config.project_id:
            raise ToolError("Project is not accessible through this local proxy.")
        return value

    @server.tool(annotations=read)
    def list_projects() -> list[dict[str, Any]]:
        """List only the single locally configured project."""
        projects = relay("list_projects").get("projects")
        if not isinstance(projects, list) or any(
            not isinstance(item, dict) for item in projects
        ):
            raise ToolError("Invalid paired read response.")
        return [
            item for item in projects if item.get("project_id") == config.project_id
        ]

    @server.tool(annotations=read)
    def get_project_context(project_id: Identifier) -> dict[str, Any]:
        """Read excerpts for the configured project; reject arbitrary project IDs."""
        if project_id != config.project_id:
            raise ToolError("Project is not accessible through this local proxy.")
        return scoped(relay("get_project_context"))

    @server.tool(annotations=read)
    def get_run(run_id: Identifier) -> dict[str, Any]:
        """Read a run only when it belongs to the configured project."""
        value = scoped(relay("get_run", run_id=run_id))
        if value.get("id") != run_id:
            raise ToolError("Invalid paired run response.")
        return value

    return server
