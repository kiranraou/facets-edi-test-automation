"""Tiny .env loader (no python-dotenv dependency). Existing environment variables always win."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path) -> bool:
    path = Path(path)
    if not path.is_file():
        return False
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return True


def workspace_client(profile: str | None = None):
    """WorkspaceClient from .env / env vars (DATABRICKS_HOST + DATABRICKS_TOKEN), else a CLI profile."""
    from databricks.sdk import WorkspaceClient

    host, token = os.getenv("DATABRICKS_HOST"), os.getenv("DATABRICKS_TOKEN")
    if host and token:
        return WorkspaceClient(host=host, token=token, auth_type="pat")
    return WorkspaceClient(profile=profile)
