"""Bounded, no-follow file export helpers, independent of the legacy checkout."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath

FILE_LIMIT = 131_072
TOTAL_LIMIT = 524_288
SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bAKIA[A-Z0-9]{16}\b"
    r"|(?im:^\s*(?:[A-Z_]*(?:API_KEY|TOKEN|PASSWORD|SECRET)|authorization)\s*[:=]\s*[^\s\n]{8,})"
    r"|(?i:[\"'](?:[A-Z_]*(?:API_KEY|TOKEN|PASSWORD|SECRET)|authorization)[\"']\s*:\s*[\"'][^\"'\n]{8,}[\"'])"
)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or len(value) > 1024
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or str(path) != value
        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
        or SECRET.search(value)
    ):
        raise ValueError("Select a normalized project-relative path.")
    for part in path.parts:
        name = part.lower()
        if (
            name
            in {
                ".gitattributes",
                ".gitmodules",
                ".git",
                ".codex",
                ".venv",
                "node_modules",
                "auth.json",
                "credentials.json",
                "secrets.json",
            }
            or name == ".env"
            or name.startswith((".env.", "id_rsa", "id_ed25519"))
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
        ):
            raise ValueError("Sensitive or dependency paths cannot be exported.")
    return value


def read_file(root: Path, filename: str, limit: int = FILE_LIMIT) -> bytes:
    """Copy the legacy descriptor walk: no component may be a symlink."""
    parts = PurePosixPath(relative_path(filename)).parts
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("File must be regular and within size limits.")
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("File exceeds size limits.")
            return raw
    except OSError as error:
        raise ValueError("File is missing, symlinked or inaccessible.") from error
    finally:
        os.close(directory)


def safe_text(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Only UTF-8 text can be exported.") from error
    if "\x00" in value or SECRET.search(value):
        raise ValueError("Binary or potentially secret content cannot be exported.")
    return value
