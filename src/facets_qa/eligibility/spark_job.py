"""Databricks entry point: distribute 270/271 checks across Spark workers.

Reads the Facets tables (landed by ADF in a real project; seeded by
``setup-facets`` for practice), builds one row per member with a nested
`spans` array, repartitions to N workers and runs ``process_partition``
through ``mapInPandas``. Results append to a Delta table that feeds the
dashboards.

Gateway modes:
  --gateway http  posts each 270 to a real clearinghouse endpoint
  --gateway mock  answers from a Facets snapshot shipped to every task
                  (practice mode; supports --failure-rate and --drift)

Requires pyspark (Databricks runtime); nothing here is imported by unit tests.
"""

from __future__ import annotations

import argparse
import random
import uuid
from datetime import date, datetime

from facets_qa.common.retry import RetryPolicy
from facets_qa.eligibility.gateway import HttpEligibilityGateway, MockEligibilityGateway, snapshot_from_candidates
from facets_qa.eligibility.pipeline import RESULT_SCHEMA_DDL, RunContext, _as_dict, process_partition
from facets_qa.eligibility.scenarios import negative_probes
from facets_qa.lakehouse.common import DEFAULT_SCHEMA, get_spark


def build_candidates(spark, schema: str, service_date: date, limit: int | None = None):
    from pyspark.sql import functions as F

    meme = spark.table(f"{schema}.cmc_meme_member").where("MEME_SFX = 0")
    sbsb = spark.table(f"{schema}.cmc_sbsb_subsc")
    mepe = spark.table(f"{schema}.cmc_mepe_prcs_elig").where("CSPD_CAT = 'M'")

    spans = (mepe.groupBy("MEME_CK").agg(F.collect_list(F.struct(
        F.col("MEPE_EFF_DT").cast("date").alias("eff_dt"),
        F.col("MEPE_TERM_DT").cast("date").alias("term_dt"),
        F.col("MEPE_ELIG_IND").alias("elig_ind"),
        F.col("CSPD_CAT").alias("cspd_cat"),
        F.col("PDPD_ID").alias("pdpd_id"),
    )).alias("spans")))

    df = (meme.join(sbsb, "SBSB_CK").join(spans, "MEME_CK", "left")
          .where(F.col("MEME_CK") < 700_000_000)  # exclude members replicated by the claims suite
          .select(
              F.col("MEME_CK").cast("long").alias("meme_ck"),
              F.col("SBSB_ID").alias("member_id"),
              F.col("MEME_FIRST_NAME").alias("first_name"),
              F.col("MEME_LAST_NAME").alias("last_name"),
              F.col("MEME_BIRTH_DT").cast("date").alias("dob"),
              F.col("MEME_SEX").alias("gender"),
              F.lit(service_date).alias("service_date"),
              F.lit(True).alias("in_facets"),
              F.lit(None).cast("string").alias("probe"),
              F.coalesce("spans", F.array().cast(spans.schema["spans"].dataType)).alias("spans"),
          ))
    return df.limit(limit) if limit else df


def add_probes(spark, candidates, n_not_found: int, n_invalid: int, service_date: date):
    probes = negative_probes(n_not_found, n_invalid, service_date).to_dict("records")
    if not probes:
        return candidates
    from pyspark.sql.types import StructField, StructType

    # Probes have no MEME_CK, so relax the NOT NULL inherited from the Facets table.
    schema = StructType([StructField(f.name, f.dataType, True) for f in candidates.schema.fields])
    rows = [tuple(p[n] for n in schema.fieldNames()) for p in probes]
    return candidates.unionByName(spark.createDataFrame(rows, schema=schema))


def run(spark, schema: str, gateway: str, service_date: date, workers: int = 25, limit: int | None = None,
        gateway_url: str | None = None, token: str | None = None, failure_rate: float = 0.05,
        drift: int = 10, not_found_probes: int = 25, invalid_id_probes: int = 25) -> str:
    ctx = RunContext(run_id=f"ELIG-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}",
                     run_ts=datetime.now(), service_date=service_date,
                     retry=RetryPolicy(max_attempts=5, base_delay=0.05 if gateway == "mock" else 1.0,
                                       max_delay=1.0 if gateway == "mock" else 16.0))

    candidates = build_candidates(spark, schema, service_date, limit)
    snapshot, drift_ids = {}, frozenset()
    if gateway == "mock":
        # Small enough to ship to every task (20k members ~ a few MB); collected once on the driver.
        records = [_as_dict(r) for r in candidates.collect()]
        snapshot = snapshot_from_candidates(records)
        drift_ids = frozenset(random.Random(7).sample(sorted(snapshot), min(drift, len(snapshot))))
    elif not gateway_url:
        raise ValueError("--gateway-url is required with --gateway http")

    def per_partition(batches):
        try:
            from pyspark import TaskContext

            worker_id = TaskContext.get().partitionId()
        except Exception:
            worker_id = 0
        gw = (MockEligibilityGateway(snapshot, transient_failure_rate=failure_rate,
                                     drift_member_ids=drift_ids, seed=worker_id)
              if gateway == "mock" else HttpEligibilityGateway(gateway_url, token))
        for pdf in batches:
            yield process_partition(pdf, ctx, gw, worker_id)

    all_rows = add_probes(spark, candidates, not_found_probes, invalid_id_probes, service_date)
    results = all_rows.repartition(workers).mapInPandas(per_partition, schema=RESULT_SCHEMA_DDL)
    results.write.mode("append").insertInto(f"{schema}.elig_test_results")
    return ctx.run_id


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--schema", default=DEFAULT_SCHEMA)
    p.add_argument("--gateway", choices=["mock", "http"], default="mock")
    p.add_argument("--gateway-url")
    p.add_argument("--token-secret-scope", default="facets-qa")
    p.add_argument("--workers", type=int, default=25)
    p.add_argument("--service-date", type=date.fromisoformat, default=date.today())
    p.add_argument("--limit", type=int)
    p.add_argument("--failure-rate", type=float, default=0.05)
    p.add_argument("--drift", type=int, default=10)
    args = p.parse_args()

    spark = get_spark()
    token = None
    if args.gateway == "http":
        try:
            from databricks.sdk import WorkspaceClient
            import base64

            secret = WorkspaceClient().secrets.get_secret(args.token_secret_scope, "gateway-token")
            token = base64.b64decode(secret.value).decode()
        except Exception as exc:
            print(f"No gateway token found ({exc}); calling without auth")
    run_id = run(spark, args.schema, args.gateway, args.service_date, args.workers, args.limit,
                 args.gateway_url, token, args.failure_rate, args.drift)
    summary = spark.sql(f"SELECT result, COUNT(*) n FROM {args.schema}.elig_test_results "
                        f"WHERE run_id = '{run_id}' GROUP BY result ORDER BY result").collect()
    print(f"Completed eligibility run {run_id}: " + ", ".join(f"{r.result}={r.n}" for r in summary))


if __name__ == "__main__":
    main()
