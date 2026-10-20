"""PASS / FAIL / ERROR decision: compare the parsed 271 to the Facets-derived expectation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from facets_qa.common.dates import FACETS_HIGH_DATE
from facets_qa.eligibility.models import Eligibility271Response, ExpectedEligibility

Result = Literal["PASS", "FAIL", "ERROR"]


@dataclass(frozen=True)
class Decision:
    result: Result
    reason: Optional[str] = None


def decide(
    expected: Optional[ExpectedEligibility],
    actual: Optional[Eligibility271Response],
    sent_trace: str,
    sent_member_id: str,
    error: Optional[str] = None,
) -> Decision:
    if error is not None or actual is None or expected is None:
        return Decision("ERROR", error or "No 271 response")

    problems: list[str] = []
    if actual.trace_number != sent_trace:
        problems.append(f"TRN02 trace {actual.trace_number!r} does not echo {sent_trace!r}")
    if actual.member_id is not None and actual.member_id != sent_member_id:
        problems.append(f"NM109 member id {actual.member_id!r} != sent {sent_member_id!r}")

    status = actual.coverage_status
    if status != expected.status:
        problems.append(f"expected {expected.status} ({expected.scenario.value}) but 271 says {status}")
    elif expected.status == "REJECTED":
        if actual.primary_reject_reason != expected.aaa_reason:
            problems.append(f"expected AAA reason {expected.aaa_reason} but got {actual.primary_reject_reason}")
    elif expected.status == "ACTIVE":
        eb = actual.active_benefit()
        if eb and eb.coverage_start and eb.coverage_start != expected.coverage_start:
            problems.append(f"coverage start {eb.coverage_start} != Facets MEPE_EFF_DT {expected.coverage_start}")
        expected_end = None if expected.coverage_end == FACETS_HIGH_DATE else expected.coverage_end
        if eb and eb.coverage_end != expected_end:
            problems.append(f"coverage end {eb.coverage_end} != Facets MEPE_TERM_DT {expected.coverage_end}")

    return Decision("FAIL", "; ".join(problems)) if problems else Decision("PASS")
