"""Shared helpers for the Databricks jobs (serverless / Spark Connect safe)."""

from __future__ import annotations

from facets_qa.facets.db import read_sql

DEFAULT_SCHEMA = "workspace.facets_qa"


def run_sql_file(spark, name: str, schema: str) -> None:
    """Execute a ;-separated SQL file from facets_qa/sql with __SCHEMA__ substituted."""
    for stmt in read_sql(name).replace("__SCHEMA__", schema).split(";"):
        body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
        if body:
            spark.sql(body)


def insert_rows(spark, table: str, rows: list[tuple], overwrite: bool = False) -> int:
    """Insert positional tuples into an existing Delta table, keeping its schema and comments."""
    schema = spark.table(table).schema
    df = spark.createDataFrame(rows, schema=schema)
    df.write.insertInto(table, overwrite=overwrite)
    return len(rows)


def get_spark():
    from pyspark.sql import SparkSession

    return SparkSession.builder.getOrCreate()
