"""Databricks entry point for the 837P/837I claims suite.

The scenario run itself is single-threaded and small, so it executes on the
driver against an in-memory intake, then publishes the resulting Facets claim
rows and test results to Delta:

* claim tables (CMC_CLCL_CLAIM / CMC_CDML_CL_LINE / CMC_CLHP_HOSP / CMC_PRPR_PROV)
  are overwritten: they represent the current state of the test region
* members replicated for claims (MEME_CK 7xxxxxxxx) are refreshed in place
* claim_test_results is appended (run history)
"""

from __future__ import annotations

import argparse
import uuid
from datetime import date, datetime, timedelta

from facets_qa.claims.pipeline import load_results, run_all
from facets_qa.facets import db as facets_db
from facets_qa.lakehouse.common import DEFAULT_SCHEMA, get_spark, insert_rows

CLAIM_TABLES = ["CMC_PRPR_PROV", "CMC_CLCL_CLAIM", "CMC_CDML_CL_LINE", "CMC_CLHP_HOSP"]
MEMBER_TABLES = ["CMC_SBSB_SUBSC", "CMC_MEME_MEMBER", "CMC_MEPE_PRCS_ELIG"]
MEMBER_KEY = {"CMC_SBSB_SUBSC": "SBSB_CK", "CMC_MEME_MEMBER": "MEME_CK", "CMC_MEPE_PRCS_ELIG": "MEME_CK"}
CLAIM_MEMBER_RANGE = (700_000_000, 800_000_000)


def _rows(conn, table: str, spark_schema) -> list[tuple]:
    """SQLite rows -> tuples typed for the Delta table (dates/decimals as Python objects)."""
    from decimal import Decimal

    from pyspark.sql.types import DateType, DecimalType, TimestampType

    converters = []
    for f in spark_schema.fields:
        if isinstance(f.dataType, DateType):
            converters.append(lambda v: None if v is None else date.fromisoformat(str(v)[:10]))
        elif isinstance(f.dataType, TimestampType):
            converters.append(lambda v: None if v is None else datetime.fromisoformat(str(v)))
        elif isinstance(f.dataType, DecimalType):
            converters.append(lambda v: None if v is None else Decimal(str(v)))
        else:
            converters.append(lambda v: v)
    return [tuple(c(v) for c, v in zip(converters, row)) for row in conn.execute(f"SELECT * FROM {table}")]


def publish(spark, conn, schema: str) -> dict[str, int]:
    counts = {}
    for t in CLAIM_TABLES:
        target = f"{schema}.{t.lower()}"
        counts[t] = insert_rows(spark, target, _rows(conn, t, spark.table(target).schema), overwrite=True)
    lo, hi = CLAIM_MEMBER_RANGE
    for t in MEMBER_TABLES:
        target = f"{schema}.{t.lower()}"
        spark.sql(f"DELETE FROM {target} WHERE {MEMBER_KEY[t]} >= {lo} AND {MEMBER_KEY[t]} < {hi}")
        counts[t] = insert_rows(spark, target, _rows(conn, t, spark.table(target).schema))
    target = f"{schema}.claim_test_results"
    counts["claim_test_results"] = insert_rows(
        spark, target, _rows(conn, "CLAIM_TEST_RESULTS", spark.table(target).schema))
    return counts


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--schema", default=DEFAULT_SCHEMA)
    p.add_argument("--dos", type=date.fromisoformat, default=date.today() - timedelta(days=14))
    p.add_argument("--inject-defect", action="append", default=[], metavar="CLAIM_ID=DEFECT")
    args = p.parse_args()

    conn = facets_db.connect(":memory:")
    facets_db.create_schema(conn)
    run_id = f"CLM-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
    results = run_all(conn, args.dos, dict(d.split("=", 1) for d in args.inject_defect))
    load_results(conn, run_id, results)
    counts = publish(get_spark(), conn, args.schema)
    passed = sum(r.result == "PASS" for r in results)
    print(f"Claims run {run_id}: {passed}/{len(results)} scenarios passed; published {counts}")


if __name__ == "__main__":
    main()
