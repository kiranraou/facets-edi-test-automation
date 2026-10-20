"""Read-only data access for the Streamlit app: Databricks SQL warehouse or local SQLite."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import pandas as pd

CLAIM_TABLES = {"cmc_clcl_claim", "cmc_cdml_cl_line", "cmc_clhp_hosp", "cmc_prpr_prov", "claim_test_results"}
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_\-\.]{1,80}$")


def lit(value: str) -> str:
    """Quote a user-supplied value for inline SQL (IDs only - rejects anything unusual)."""
    if not SAFE_VALUE.match(str(value)):
        raise ValueError(f"Unsupported characters in {value!r}")
    return f"'{value}'"


class LocalSource:
    label = "Local mock Facets (SQLite)"

    def __init__(self, out_dir: Path):
        self.elig_db = out_dir / "facets_mock.db"
        self.claims_db = out_dir / "facets_claims_mock.db"

    def available(self) -> bool:
        return self.elig_db.exists()

    def t(self, name: str) -> str:
        if name in CLAIM_TABLES and self.claims_db.exists():
            return f"claims.{name}"
        return name

    def query(self, sql: str) -> pd.DataFrame:
        conn = sqlite3.connect(f"file:{self.elig_db}?mode=ro", uri=True)
        try:
            if self.claims_db.exists():
                conn.execute(f"ATTACH DATABASE 'file:{self.claims_db}?mode=ro' AS claims")
            df = pd.read_sql_query(sql, conn)
        finally:
            conn.close()
        df.columns = [c.lower() for c in df.columns]
        return df


class DatabricksSource:
    def __init__(self, schema: str):
        from databricks.sdk.core import Config

        self.schema = schema
        host, token = os.getenv("DATABRICKS_HOST"), os.getenv("DATABRICKS_TOKEN")
        # Databricks Apps: platform-provided service principal; locally: token from .env
        self.cfg = Config(host=host, token=token, auth_type="pat") if host and token else Config()
        self.warehouse_id = os.getenv("DATABRICKS_WAREHOUSE_ID") or self._first_warehouse()
        self.label = f"Databricks · {schema}"

    def _first_warehouse(self) -> str:
        from databricks.sdk import WorkspaceClient

        return next(iter(WorkspaceClient(config=self.cfg).warehouses.list())).id

    def available(self) -> bool:
        return True

    def t(self, name: str) -> str:
        return f"{self.schema}.{name}"

    def query(self, sql: str) -> pd.DataFrame:
        """Statement Execution API (HTTPS only - no thrift session to keep alive)."""
        import time

        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service.sql import StatementState

        w = WorkspaceClient(config=self.cfg)
        resp = w.statement_execution.execute_statement(statement=sql, warehouse_id=self.warehouse_id,
                                                       wait_timeout="50s")
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(1)
            resp = w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(f"{resp.status.state.value}: {resp.status.error.message if resp.status.error else ''}")

        cols = resp.manifest.schema.columns
        rows = list(resp.result.data_array or []) if resp.result else []
        chunk = resp.result.next_chunk_index if resp.result else None
        while chunk is not None:
            part = w.statement_execution.get_statement_result_chunk_n(resp.statement_id, chunk)
            rows += part.data_array or []
            chunk = part.next_chunk_index
        df = pd.DataFrame(rows, columns=[c.name.lower() for c in cols])
        for c in cols:  # JSON results arrive as strings
            kind = c.type_name.value if c.type_name else ""
            if kind in ("BYTE", "SHORT", "INT", "LONG", "FLOAT", "DOUBLE", "DECIMAL"):
                df[c.name.lower()] = pd.to_numeric(df[c.name.lower()])
        return df
