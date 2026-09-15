from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import Mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from planrelay_bridge.auth import Settings, create_verifier


def local_settings(tmp_path: Path) -> Settings:
    return Settings(
        public_url="http://127.0.0.1:8787/mcp",
        issuer="http://127.0.0.1:8787",
        owner_subject="local-owner",
        local_token=SecretStr("a" * 48),
        database=tmp_path / "relay.sqlite",
    )


def test_local_auth_requires_correct_secret(tmp_path: Path) -> None:
    verifier = create_verifier(local_settings(tmp_path))
    assert asyncio.run(verifier.verify_token("wrong")) is None
    valid = asyncio.run(verifier.verify_token("a" * 48))
    assert valid and valid.subject == "local-owner"
    assert valid.resource == "http://127.0.0.1:8787/mcp"


@pytest.mark.parametrize(
    "change",
    [
        {"host": "0.0.0.0"},
        {"public_url": "https://relay.example/mcp"},
        {"local_token": SecretStr("short")},
        {"issuer": "https://evil.example"},
    ],
)
def test_local_static_secret_never_allowed_publicly(
    tmp_path: Path, change: dict[str, object]
) -> None:
    data = local_settings(tmp_path).model_dump()
    data.update(change)
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_production_auth_requires_complete_https_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            public_url="https://relay.example/mcp",
            issuer="https://auth.example/",
            owner_subject="owner",
            database=tmp_path / "db",
        )


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"sub": "stranger"},
        {"aud": "https://another.example/mcp"},
        {"iss": "https://evil.example/"},
        {"exp": 1},
        {"scope": "unrelated"},
    ],
)
def test_jwt_owner_audience_expiry_scope_verified(
    tmp_path: Path, change: dict[str, object]
) -> None:
    settings = Settings(
        public_url="https://relay.example/mcp",
        issuer="https://auth.example/",
        jwks_url="https://auth.example/.well-known/jwks.json",
        owner_subject="owner",
        host="0.0.0.0",
        database=tmp_path / "db",
    )
    verifier = create_verifier(settings)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier.jwks = Mock()
    verifier.jwks.get_signing_key_from_jwt.return_value.key = key.public_key()
    claims = {
        "sub": "owner",
        "aud": settings.public_url,
        "iss": settings.issuer,
        "exp": int(time.time()) + 60,
        "iat": int(time.time()),
        "scope": "planrelay",
    }
    claims.update(change)
    raw = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
    result = asyncio.run(verifier.verify_token(raw))
    assert (result is not None) == (not change)
    assert asyncio.run(verifier.verify_token("broken")) is None


def test_cancelled_checks_keep_native_work_bounded(tmp_path: Path) -> None:
    import threading

    settings = Settings(
        public_url="https://relay.example/mcp",
        issuer="https://auth.example/",
        jwks_url="https://auth.example/jwks",
        owner_subject="owner",
        database=tmp_path / "db",
    )
    verifier = create_verifier(settings)
    release = threading.Event()
    lock = threading.Lock()
    active = [0, 0]

    def check(token: str) -> None:
        with lock:
            active[0] += 1
            active[1] = max(active)
        try:
            release.wait(timeout=3)
        finally:
            with lock:
                active[0] -= 1

    verifier._verify_jwt = check  # type: ignore[method-assign]

    async def scenario() -> None:
        tasks = []
        try:
            for _ in range(8):
                task = asyncio.create_task(verifier.verify_token("opaque"))
                tasks.append(task)
                await asyncio.sleep(0.02)
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            release.set()

    asyncio.run(scenario())
    assert active[1] <= 4
