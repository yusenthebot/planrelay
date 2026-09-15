"""OAuth resource-server verification. Local test tokens stay loopback-only."""

from __future__ import annotations

import asyncio
import hmac
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jwt
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


def loopback_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    public_url: str
    issuer: str
    jwks_url: str | None = None
    owner_subject: str = Field(min_length=1, max_length=256)
    local_token: SecretStr | None = None
    database: Path = Path("relay.sqlite")
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)

    @model_validator(mode="after")
    def secure_configuration(self) -> Settings:
        for value in (self.public_url, self.issuer, self.jwks_url):
            if value is None:
                continue
            parsed = urlsplit(value)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "URLs require a host, without credentials or query strings."
                )
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and loopback_url(value)
            ):
                raise ValueError(
                    "HTTPS is required except for literal local development endpoints."
                )
        if urlsplit(self.public_url).path != "/mcp":
            raise ValueError("The public MCP endpoint must use /mcp.")
        if self.local_token is not None:
            if (
                self.host not in {"127.0.0.1", "localhost", "::1"}
                or urlsplit(self.public_url).scheme != "http"
                or not loopback_url(self.public_url)
                or not loopback_url(self.issuer)
                or len(self.local_token.get_secret_value()) < 32
                or self.jwks_url is not None
            ):
                raise ValueError(
                    "Local test-token mode requires loopback-only binding and URLs."
                )
        elif not self.jwks_url or any(
            urlsplit(value).scheme != "https"
            for value in (self.public_url, self.issuer, self.jwks_url)
        ):
            raise ValueError(
                "Public mode requires HTTPS MCP, OAuth issuer, JWKS and owner identity."
            )
        return self

    @classmethod
    def from_environment(cls) -> Settings:
        required = {
            "public_url": "PLANRELAY_PUBLIC_URL",
            "issuer": "PLANRELAY_OAUTH_ISSUER",
            "owner_subject": "PLANRELAY_OWNER_SUBJECT",
        }
        values: dict[str, Any] = {}
        for field, name in required.items():
            value = os.environ.get(name)
            if not value:
                raise ValueError(f"Missing required configuration: {name}")
            values[field] = value
        if value := os.environ.get("PLANRELAY_JWKS_URL"):
            values["jwks_url"] = value
        if value := os.environ.get("PLANRELAY_LOCAL_TOKEN"):
            values["local_token"] = SecretStr(value)
        values["database"] = Path(os.environ.get("PLANRELAY_DATABASE", "relay.sqlite"))
        values["host"] = os.environ.get("PLANRELAY_HOST", "127.0.0.1")
        values["port"] = int(os.environ.get("PLANRELAY_PORT", "8787"))
        return cls.model_validate(values)


class Verifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jwks = (
            jwt.PyJWKClient(settings.jwks_url, cache_keys=True, timeout=5)
            if settings.jwks_url
            else None
        )
        self._slots = threading.BoundedSemaphore(4)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jwt-check")

    def _bounded_verify(self, token: str) -> AccessToken | None:
        try:
            return self._verify_jwt(token)
        finally:
            self._slots.release()

    def _verify_jwt(self, token: str) -> AccessToken | None:
        if self.jwks is None:
            return None
        header = jwt.get_unverified_header(token)
        if (
            header.get("alg") != "RS256"
            or not isinstance(header.get("kid"), str)
            or len(header["kid"]) > 128
        ):
            return None
        key = self.jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self.settings.public_url,
            issuer=self.settings.issuer,
            options={"require": ["exp", "iat", "iss", "sub", "aud"]},
        )
        scope = claims.get("scope", "")
        if (
            claims.get("sub") != self.settings.owner_subject
            or not isinstance(scope, str)
            or "planrelay" not in scope.split()
        ):
            return None
        return AccessToken(
            token=token,
            client_id="oauth-client",
            subject=claims["sub"],
            scopes=scope.split(),
            expires_at=int(claims["exp"]),
            resource=self.settings.public_url,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            return None
        if self.settings.local_token is not None:
            if not hmac.compare_digest(
                token.encode(), self.settings.local_token.get_secret_value().encode()
            ):
                return None
            return AccessToken(
                token=token,
                client_id="local-development",
                subject=self.settings.owner_subject,
                scopes=["planrelay"],
                resource=self.settings.public_url,
            )
        try:
            if not self._slots.acquire(blocking=False):
                return None
            try:
                future = self._pool.submit(self._bounded_verify, token)
            except RuntimeError:
                self._slots.release()
                return None
            async with asyncio.timeout(6):
                # Cancellation cannot release a native worker's capacity early.
                wrapped = asyncio.wrap_future(future)
                wrapped.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
                return await asyncio.shield(wrapped)
        except (jwt.PyJWTError, ValueError, UnicodeError, TimeoutError, OSError):
            return None


def create_verifier(settings: Settings) -> Verifier:
    return Verifier(settings)
