"""Connection helpers for the local mock Facets database (SQLite).

In Databricks the same tables arrive through the Azure Data Factory extract
(see adf/ and facets_qa.lakehouse); locally we create them from sql/facets_ddl.sql.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript((SQL_DIR / "facets_ddl.sql").read_text())
    conn.commit()


def read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text()
