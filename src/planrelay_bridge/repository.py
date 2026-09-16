"""Canonical GitHub identity without credentials, network access or URL rewriting."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def github_name(value: str) -> str:
    """Validate a declared owner/repository; this does not prove remote ownership."""
    parts = value.split("/")
    if (
        len(parts) != 2
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", parts[0])
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1])
        or parts[1] in {".", ".."}
    ):
        raise ValueError("GitHub identity must be owner/repository.")
    return value.lower()


def github_from_url(value: str) -> str:
    """Accept explicit github.com HTTPS or Git SSH origins, never secret URLs."""
    if len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Invalid GitHub origin.")
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(value)
        if (
            parsed.hostname != "github.com"
            or parsed.scheme not in {"https", "ssh"}
            or "?" in value
            or "#" in value
            or parsed.password is not None
            or (parsed.scheme == "https" and parsed.username is not None)
            or (parsed.scheme == "ssh" and parsed.username != "git")
            or parsed.port is not None
            or not parsed.path.startswith("/")
        ):
            raise ValueError("Use an explicit credential-free github.com origin.")
        path = parsed.path.removeprefix("/")
    return github_name(path.removesuffix(".git"))
