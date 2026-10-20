from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from facets_qa.common.dates import FACETS_HIGH_DATE
from facets_qa.common.retry import RetryPolicy
from facets_qa.eligibility.decision import decide
from facets_qa.eligibility.gateway import MockEligibilityGateway, snapshot_from_candidates
from facets_qa.eligibility.models import (
    AAARejection, BenefitLoop, Eligibility271Response, ExpectedEligibility, Scenario,
)
from facets_qa.eligibility.pipeline import (
    RESULT_COLUMNS, RunContext, extract_candidates, load_results, process_partition, run_local, summarize,
)
from facets_qa.eligibility.scenarios import negative_probes
from facets_qa.facets.seed import seed_members


def active_resp(trace="T1", start=date(2025, 1, 1), end=None, member_id="800000001"):
    return Eligibility271Response(trace_number=trace, member_id=member_id,
                                  benefits=(BenefitLoop(eligibility_code="1", coverage_start=start,
                                                        coverage_end=end),))


EXP_ACTIVE = ExpectedEligibility(status="ACTIVE", scenario=Scenario.ACTIVE,
                                 coverage_start=date(2025, 1, 1), coverage_end=FACETS_HIGH_DATE)


# --------------------------------------------------------------------- decision
@pytest.mark.decision
def test_active_matches():
    assert decide(EXP_ACTIVE, active_resp(), "T1", "800000001").result == "PASS"


@pytest.mark.decision
@pytest.mark.parametrize("resp,trace,fragment", [
    (active_resp(trace="OTHER"), "T1", "does not echo"),
    (active_resp(member_id="800000999"), "T1", "member id"),
    (active_resp(start=date(2025, 2, 1)), "T1", "coverage start"),
    (active_resp(end=date(2026, 12, 31)), "T1", "coverage end"),
    (Eligibility271Response(trace_number="T1", benefits=(BenefitLoop(eligibility_code="6"),)), "T1",
     "expected ACTIVE (active) but 271 says INACTIVE"),
])
def test_active_failures(resp, trace, fragment):
    d = decide(EXP_ACTIVE, resp, trace, "800000001")
    assert d.result == "FAIL" and fragment in d.reason


@pytest.mark.decision
@pytest.mark.parametrize("expected_aaa,actual_aaa,result", [("72", "72", "PASS"), ("72", "75", "FAIL"),
                                                            ("75", "75", "PASS")])
def test_rejection_reason_must_match(expected_aaa, actual_aaa, result):
    exp = ExpectedEligibility(status="REJECTED", scenario=Scenario.INVALID_MEMBER_ID, aaa_reason=expected_aaa)
    resp = Eligibility271Response(trace_number="T1", rejections=(
        AAARejection(loop="2100C", valid_request="N", reject_reason=actual_aaa),))
    assert decide(exp, resp, "T1", "INV00001").result == result


@pytest.mark.decision
def test_error_when_no_response():
    d = decide(EXP_ACTIVE, None, "T1", "800000001", error="gateway: timeout")
    assert (d.result, d.reason) == ("ERROR", "gateway: timeout")


# --------------------------------------------------------------------- partition / pipeline (no Spark)
def ctx(service_date, attempts=4):
    return RunContext(run_id="TESTRUN0001", run_ts=datetime(2026, 1, 1), service_date=service_date,
                      retry=RetryPolicy(max_attempts=attempts, base_delay=0, jitter=0))


@pytest.mark.integration
def test_process_partition_happy_path(candidate_row, snapshot, service_date, no_sleep):
    out = process_partition(pd.DataFrame([candidate_row]), ctx(service_date), MockEligibilityGateway(snapshot),
                            sleep=no_sleep)
    assert list(out.columns) == RESULT_COLUMNS
    row = out.iloc[0]
    assert (row.result, row.expected_status, row.actual_status, row.attempts) == ("PASS", "ACTIVE", "ACTIVE", 1)


@pytest.mark.integration
@pytest.mark.retry
def test_transient_failures_are_retried_then_error(candidate_row, snapshot, service_date, no_sleep):
    always_down = MockEligibilityGateway(snapshot, transient_failure_rate=1.0)
    row = process_partition(pd.DataFrame([candidate_row]), ctx(service_date, attempts=3), always_down,
                            sleep=no_sleep).iloc[0]
    assert (row.result, row.attempts) == ("ERROR", 3)
    assert "Gave up after 3 attempts" in row.failure_reason
    assert len(no_sleep.calls) == 2


@pytest.mark.integration
def test_bad_source_row_becomes_contract_error(candidate_row, snapshot, service_date, no_sleep):
    candidate_row["spans"][0]["elig_ind"] = "?"
    row = process_partition(pd.DataFrame([candidate_row]), ctx(service_date), MockEligibilityGateway(snapshot),
                            sleep=no_sleep).iloc[0]
    assert row.result == "ERROR" and row.failure_reason.startswith("data contract")


@pytest.mark.integration
def test_dob_mismatch_in_payer_system_is_caught(candidate_row, snapshot, service_date, no_sleep):
    candidate_row["dob"] = candidate_row["dob"] - timedelta(days=1)  # Facets extract differs from gateway
    row = process_partition(pd.DataFrame([candidate_row]), ctx(service_date), MockEligibilityGateway(snapshot),
                            sleep=no_sleep).iloc[0]
    assert (row.result, row.actual_status, row.actual_aaa) == ("FAIL", "REJECTED", "71")


@pytest.mark.integration
def test_end_to_end_against_seeded_facets(facets_conn, service_date, no_sleep):
    seed_members(facets_conn, 600, service_date, seed=11)
    candidates = extract_candidates(facets_conn, service_date)
    rows = pd.concat([candidates, negative_probes(5, 5, service_date)], ignore_index=True)
    snap = snapshot_from_candidates(candidates.to_dict("records"))
    drift = frozenset(sorted(snap)[:3])
    gateway = MockEligibilityGateway(snap, transient_failure_rate=0.2, drift_member_ids=drift)

    results = run_local(rows, ctx(service_date, attempts=10), gateway, workers=25, sleep=no_sleep)

    assert len(results) == 610 and results["worker_id"].nunique() == 25
    fails = results[results.result != "PASS"]
    assert set(fails.member_id) == set(drift)            # only the injected defects fail
    assert (results.attempts > 1).any()                  # retries actually happened
    scenarios = set(results.scenario)
    assert {"active", "terminated", "future_effective", "elig_ind_off",
            "member_not_found", "invalid_member_id"} <= scenarios
    assert set(results.loc[results.scenario == "invalid_member_id", "actual_aaa"]) == {"72"}
    assert set(results.loc[results.scenario == "member_not_found", "actual_aaa"]) == {"75"}

    load_results(facets_conn, results)
    loaded = facets_conn.execute("SELECT COUNT(*), SUM(RESULT='FAIL') FROM ELIG_TEST_RESULTS").fetchone()
    assert tuple(loaded) == (610, 3)
    assert summarize(results).loc["invalid_member_id", "pass_rate_%"] == 100.0
