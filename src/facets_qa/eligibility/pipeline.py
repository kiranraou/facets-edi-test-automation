"""End-to-end 270/271 eligibility run.

extract (Facets) -> candidates (Pydantic) -> expected (from MEPE spans)
  -> build 270 -> send with retry -> parse 271 -> decide PASS/FAIL -> load results

``process_partition`` is the unit of parallel work. Locally it runs on a
thread pool; on Databricks the same function runs inside ``mapInPandas``
across ``repartition(workers)`` partitions (see spark_job.py).
"""

from __future__ import annotations

import argparse
import itertools
import os
import random
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from pydantic import ValidationError

from facets_qa.common.retry import RetryExhausted, RetryPolicy, call_with_retry
from facets_qa.common.x12 import X12ParseError
from facets_qa.eligibility.builder_270 import build_270, default_envelope
from facets_qa.eligibility.decision import decide
from facets_qa.eligibility.expected import derive_expected
from facets_qa.eligibility.gateway import EligibilityGateway, MockEligibilityGateway, snapshot_from_candidates
from facets_qa.eligibility.models import Eligibility270Request, FacetsCandidate
from facets_qa.eligibility.parser_271 import parse_271
from facets_qa.eligibility.scenarios import group_extract_rows, negative_probes
from facets_qa.facets import db as facets_db
from facets_qa.facets.seed import seed_members

RESULT_COLUMNS = [
    "run_id", "run_ts", "meme_ck", "member_id", "scenario", "service_date", "trace_number",
    "expected_status", "actual_status", "expected_aaa", "actual_aaa", "result", "failure_reason",
    "attempts", "latency_ms", "worker_id",
]

# Spark DDL for mapInPandas output / Delta results table.
RESULT_SCHEMA_DDL = (
    "run_id string, run_ts timestamp, meme_ck long, member_id string, scenario string, "
    "service_date date, trace_number string, expected_status string, actual_status string, "
    "expected_aaa string, actual_aaa string, result string, failure_reason string, "
    "attempts int, latency_ms long, worker_id int"
)


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_ts: datetime
    service_date: date
    provider_npi: str = "1234567893"
    provider_name: str = "FACETS QA CLINIC"
    payer_id: str = "FACETS01"
    payer_name: str = "FACETS HEALTH PLAN"
    service_type_codes: tuple[str, ...] = ("30",)
    retry: RetryPolicy = field(default_factory=RetryPolicy)


_control_numbers = itertools.count(1)


def _as_dict(row) -> dict:
    """Spark Rows (and nested Rows in `spans`) -> plain dicts."""
    d = row.asDict(recursive=True) if hasattr(row, "asDict") else dict(row)
    spans = d.get("spans")
    d["spans"] = [s.asDict() if hasattr(s, "asDict") else dict(s) for s in (spans if spans is not None else [])]
    if pd.isna(d.get("meme_ck")):
        d["meme_ck"] = None
    if d.get("probe") is not None and pd.isna(d["probe"]):
        d["probe"] = None
    return d


def check_member(row: dict, ctx: RunContext, gateway: EligibilityGateway, worker_id: int,
                 sleep: Callable[[float], None] = time.sleep, rng: Optional[random.Random] = None) -> dict:
    started = time.perf_counter()
    trace = f"{ctx.run_id[:8]}-{row['member_id']}"[:50]
    out = {
        "run_id": ctx.run_id, "run_ts": ctx.run_ts, "meme_ck": row.get("meme_ck"),
        "member_id": row["member_id"], "service_date": ctx.service_date, "trace_number": trace,
        "scenario": None, "expected_status": None, "actual_status": None, "expected_aaa": None,
        "actual_aaa": None, "result": "ERROR", "failure_reason": None, "attempts": 0,
        "latency_ms": 0, "worker_id": worker_id,
    }
    try:
        candidate = FacetsCandidate(**row)
        expected = derive_expected(candidate)
        out.update(scenario=expected.scenario.value, expected_status=expected.status,
                   expected_aaa=expected.aaa_reason)
        request = Eligibility270Request(
            trace_number=trace, payer_id=ctx.payer_id, payer_name=ctx.payer_name,
            provider_npi=ctx.provider_npi, provider_name=ctx.provider_name,
            member_id=candidate.member_id, member_first=candidate.first_name,
            member_last=candidate.last_name, member_dob=candidate.dob,
            service_date=candidate.service_date, service_type_codes=ctx.service_type_codes,
        )
        x12 = build_270(request, default_envelope(), next(_control_numbers) % 999_999_999)
        outcome = call_with_retry(lambda: gateway.send(x12), ctx.retry, sleep=sleep, rng=rng)
        out["attempts"] = outcome.attempts
        response = parse_271(outcome.value)
        out.update(actual_status=response.coverage_status, actual_aaa=response.primary_reject_reason)
        decision = decide(expected, response, sent_trace=trace, sent_member_id=candidate.member_id)
    except ValidationError as exc:
        decision = decide(None, None, trace, row["member_id"], error=f"data contract: {exc.errors()[0]['msg']}")
    except RetryExhausted as exc:
        out["attempts"] = exc.attempts
        decision = decide(None, None, trace, row["member_id"], error=f"gateway: {exc}")
    except X12ParseError as exc:
        decision = decide(None, None, trace, row["member_id"], error=f"271 parse: {exc}")
    out["result"], out["failure_reason"] = decision.result, decision.reason
    out["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return out


def process_partition(pdf: pd.DataFrame, ctx: RunContext, gateway: EligibilityGateway,
                      worker_id: int = 0, sleep: Callable[[float], None] = time.sleep) -> pd.DataFrame:
    rng = random.Random(worker_id)
    rows = [check_member(_as_dict(r), ctx, gateway, worker_id, sleep, rng)
            for r in pdf.to_dict("records")]
    out = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    out["meme_ck"] = out["meme_ck"].astype("Int64")
    return out


def run_local(candidates: pd.DataFrame, ctx: RunContext, gateway: EligibilityGateway,
              workers: int = 25, sleep: Callable[[float], None] = time.sleep) -> pd.DataFrame:
    n = max(1, min(workers, len(candidates)))
    chunks = [candidates.iloc[i::n] for i in range(n)]  # round-robin, like repartition
    with ThreadPoolExecutor(max_workers=n) as pool:
        parts = list(pool.map(lambda ic: process_partition(ic[1], ctx, gateway, ic[0], sleep), enumerate(chunks)))
    return pd.concat(parts, ignore_index=True)


# --------------------------------------------------------------------- Facets I/O
def extract_candidates(conn: sqlite3.Connection, service_date: date, limit: Optional[int] = None) -> pd.DataFrame:
    rows = [dict(r) for r in conn.execute(facets_db.read_sql("extract_eligibility_candidates.sql"))]
    df = group_extract_rows(rows, service_date)
    return df.head(limit) if limit else df


def load_results(conn: sqlite3.Connection, results: pd.DataFrame) -> None:
    """Load results back into the Facets QA results table."""
    def iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v
    rows = [
        (r.run_id, iso(r.run_ts), None if pd.isna(r.meme_ck) else int(r.meme_ck), r.member_id, r.scenario or "unknown",
         iso(r.service_date), r.trace_number, r.expected_status or "UNKNOWN", r.actual_status,
         r.expected_aaa, r.actual_aaa, r.result, r.failure_reason, int(r.attempts), int(r.latency_ms),
         int(r.worker_id))
        for r in results.itertuples(index=False)
    ]
    conn.executemany(f"INSERT INTO ELIG_TEST_RESULTS VALUES ({','.join('?' * 16)})", rows)
    conn.commit()


def summarize(results: pd.DataFrame) -> pd.DataFrame:
    summary = (results.groupby(["scenario", "result"]).size().unstack(fill_value=0)
               .reindex(columns=["PASS", "FAIL", "ERROR"], fill_value=0))
    summary["total"] = summary.sum(axis=1)
    summary["pass_rate_%"] = (100 * summary["PASS"] / summary["total"]).round(2)
    return summary


# --------------------------------------------------------------------- CLI
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Run the Facets 270/271 eligibility regression locally.")
    p.add_argument("--members", type=int, default=20_000)
    p.add_argument("--workers", type=int, default=25)
    p.add_argument("--service-date", type=date.fromisoformat, default=date.today())
    p.add_argument("--not-found-probes", type=int, default=25)
    p.add_argument("--invalid-id-probes", type=int, default=25)
    p.add_argument("--failure-rate", type=float, default=0.05, help="simulated transient gateway failure rate")
    p.add_argument("--drift", type=int, default=10, help="members the mock gateway answers wrongly (simulated defects)")
    p.add_argument("--db", default="out/facets_mock.db")
    p.add_argument("--out", default="out")
    args = p.parse_args(argv)

    Path(args.out).mkdir(parents=True, exist_ok=True)
    if os.path.exists(args.db):
        os.remove(args.db)
    conn = facets_db.connect(args.db)
    facets_db.create_schema(conn)
    seed_members(conn, args.members, args.service_date)

    candidates = extract_candidates(conn, args.service_date)
    probes = negative_probes(args.not_found_probes, args.invalid_id_probes, args.service_date)
    all_rows = pd.concat([candidates, probes], ignore_index=True)

    snapshot = snapshot_from_candidates(candidates.to_dict("records"))
    drift = frozenset(random.Random(7).sample(sorted(snapshot), min(args.drift, len(snapshot))))
    gateway = MockEligibilityGateway(snapshot, transient_failure_rate=args.failure_rate, drift_member_ids=drift)
    ctx = RunContext(run_id=f"ELIG-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}",
                     run_ts=datetime.now(), service_date=args.service_date,
                     retry=RetryPolicy(max_attempts=4, base_delay=0.01, max_delay=0.1))

    t0 = time.perf_counter()
    results = run_local(all_rows, ctx, gateway, workers=args.workers)
    elapsed = time.perf_counter() - t0
    load_results(conn, results)
    results.to_csv(Path(args.out) / f"{ctx.run_id}_results.csv", index=False)

    print(f"\nRun {ctx.run_id}: {len(results)} eligibility checks on {args.workers} workers "
          f"in {elapsed:.1f}s ({gateway.calls} gateway calls incl. retries)\n")
    print(summarize(results).to_string())
    totals = results["result"].value_counts()
    print(f"\nPASS {totals.get('PASS', 0)}  FAIL {totals.get('FAIL', 0)}  ERROR {totals.get('ERROR', 0)}  "
          f"pass rate {100 * totals.get('PASS', 0) / len(results):.2f}%")
    failures = results[results["result"] != "PASS"].head(10)
    if not failures.empty:
        print("\nSample failures:")
        print(failures[["member_id", "scenario", "expected_status", "actual_status", "failure_reason"]].to_string(index=False))
    print(f"\nResults: {args.db} (ELIG_TEST_RESULTS) and {args.out}/{ctx.run_id}_results.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
