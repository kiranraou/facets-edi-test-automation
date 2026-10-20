"""Run the claims scenario catalog end to end against the (mock) Facets database.

positive: build -> Pydantic validate -> write 837 -> replicate member/provider
          -> Facets intake -> field-level Facets validation
negative: build -> Pydantic must reject with the expected rule (never reaches Facets)
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from facets_qa.claims.facets_loader import FacetsClaimsIntake, replicate_provider, replicate_subscriber
from facets_qa.claims.facets_validation import validate_claim
from facets_qa.claims.generator import SyntheticFactory
from facets_qa.claims.models import ClaimAdapter
from facets_qa.claims.scenarios import SCENARIOS, ClaimScenario
from facets_qa.claims.writer_837 import write_837
from facets_qa.facets import db as facets_db


@dataclass
class ClaimResult:
    scenario: str
    claim_type: str
    polarity: str
    result: str
    clcl_id: Optional[str] = None
    detail: str = ""
    x12: Optional[str] = None


def run_scenario(sc: ClaimScenario, conn, intake: FacetsClaimsIntake, factory: SyntheticFactory,
                 dos: date, out_dir: Optional[Path], control: int) -> ClaimResult:
    res = ClaimResult(sc.name, sc.claim_type, sc.polarity, "FAIL")
    payload = sc.build(factory, dos)
    try:
        claim = ClaimAdapter.validate_python(payload)
    except ValidationError as exc:
        messages = "; ".join(e["msg"] for e in exc.errors())
        if sc.polarity == "negative" and sc.expected_error and sc.expected_error in messages:
            res.result, res.detail = "PASS", f"rejected before Facets: {messages}"
        else:
            res.detail = f"unexpected validation error: {messages}"
        return res

    if sc.polarity == "negative":
        res.detail = f"invalid claim was accepted (expected error: {sc.expected_error})"
        return res

    meme_ck = replicate_subscriber(conn, claim.subscriber)
    prpr_id = replicate_provider(conn, claim.billing_provider)
    x12 = res.x12 = write_837(claim, control)
    if out_dir:
        (out_dir / f"{sc.name}.{sc.claim_type}.x12").write_text(x12)
    clcl_ids = intake.load_837(x12)
    res.clcl_id = clcl_ids[0]
    mismatches = validate_claim(conn, claim, res.clcl_id, meme_ck, prpr_id)
    if mismatches:
        res.detail = " | ".join(str(m) for m in mismatches)
    else:
        res.result, res.detail = "PASS", f"{len(claim.lines)} line(s) matched in Facets"
    return res


def run_all(conn, dos: date, defects: dict[str, str] | None = None, out_dir: Optional[Path] = None,
            seed: int = 837) -> list[ClaimResult]:
    factory = SyntheticFactory(seed)
    intake = FacetsClaimsIntake(conn, defects=defects)
    return [run_scenario(sc, conn, intake, factory, dos, out_dir, i) for i, sc in enumerate(SCENARIOS, start=1)]


def load_results(conn, run_id: str, results: list[ClaimResult]) -> None:
    ts = datetime.now().isoformat()
    conn.executemany("INSERT INTO CLAIM_TEST_RESULTS VALUES (?,?,?,?,?,?,?,?,?)",
                     [(run_id, ts, r.scenario, r.claim_type, r.polarity, r.clcl_id, r.result, r.detail[:2000], r.x12)
                      for r in results])
    conn.commit()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Run the 837P/837I claims scenario suite against mock Facets.")
    p.add_argument("--db", default="out/facets_claims_mock.db")
    p.add_argument("--out", default="out")
    p.add_argument("--dos", type=date.fromisoformat, default=date.today() - timedelta(days=14))
    p.add_argument("--inject-defect", action="append", default=[], metavar="CLAIM_ID=DEFECT",
                   help="e.g. P-MOD25-001=drop_last_modifier (shows how Facets mismatches are reported)")
    args = p.parse_args(argv)

    out_dir = Path(args.out) / "837"
    out_dir.mkdir(parents=True, exist_ok=True)
    if os.path.exists(args.db):
        os.remove(args.db)
    conn = facets_db.connect(args.db)
    facets_db.create_schema(conn)

    defects = dict(item.split("=", 1) for item in args.inject_defect)
    run_id = f"CLM-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
    results = run_all(conn, args.dos, defects, out_dir)
    load_results(conn, run_id, results)
    (Path(args.out) / f"{run_id}_report.json").write_text(json.dumps([{k: v for k, v in asdict(r).items() if k != "x12"} for r in results], indent=2))

    print(f"\nRun {run_id}\n")
    for r in results:
        print(f"  {r.result:<5} {r.claim_type} {r.polarity:<8} {r.scenario:<34} {r.clcl_id or '':<13} {r.detail}")
    passed = sum(r.result == "PASS" for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed. 837 files: {out_dir}  Results: {args.db}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
