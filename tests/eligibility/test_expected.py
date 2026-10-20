"""Facets is the source of truth: expected status derived from CMC_MEPE_PRCS_ELIG spans."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from facets_qa.common.dates import FACETS_HIGH_DATE
from facets_qa.eligibility.expected import classify_scenario, derive_expected
from facets_qa.eligibility.models import EligibilitySpan, FacetsCandidate, Scenario

pytestmark = pytest.mark.decision

SVC = date(2026, 6, 15)


def span(eff_offset: int, term_offset: int | None, ind: str = "Y", cat: str = "M") -> EligibilitySpan:
    term = FACETS_HIGH_DATE if term_offset is None else SVC + timedelta(days=term_offset)
    return EligibilitySpan(eff_dt=SVC + timedelta(days=eff_offset), term_dt=term, elig_ind=ind, cspd_cat=cat)


@pytest.mark.parametrize("spans,scenario,status", [
    ([span(-100, None)], Scenario.ACTIVE, "ACTIVE"),
    ([span(-100, 0)], Scenario.ACTIVE, "ACTIVE"),               # term date is inclusive
    ([span(0, None)], Scenario.ACTIVE, "ACTIVE"),               # eff date is inclusive
    ([span(-400, -1)], Scenario.TERMINATED, "INACTIVE"),
    ([span(1, None)], Scenario.FUTURE_EFFECTIVE, "INACTIVE"),
    ([span(-100, None, ind="N")], Scenario.ELIG_IND_OFF, "INACTIVE"),
    ([span(-400, -100), span(-50, None)], Scenario.REINSTATED, "ACTIVE"),
    ([span(-400, -10), span(10, None)], Scenario.COVERAGE_GAP, "INACTIVE"),
    ([span(-100, None, cat="D")], Scenario.NO_ELIGIBILITY, "INACTIVE"),  # dental-only span ignored
    ([], Scenario.NO_ELIGIBILITY, "INACTIVE"),
])
def test_expected_status_from_spans(spans, scenario, status):
    c = FacetsCandidate(meme_ck=1, member_id="800000001", first_name="A", last_name="B",
                        dob=date(1980, 1, 1), service_date=SVC, spans=tuple(spans))
    exp = derive_expected(c)
    assert classify_scenario(c.spans, SVC) == scenario
    assert exp.status == status
    if status == "ACTIVE":
        assert exp.coverage_start is not None


@pytest.mark.parametrize("probe,reason", [("member_not_found", "75"), ("invalid_member_id", "72")])
def test_negative_probes_expect_aaa(probe, reason):
    c = FacetsCandidate(member_id="INV00001", first_name="P", last_name="N", dob=date(1980, 1, 1),
                        service_date=SVC, in_facets=False, probe=probe)
    exp = derive_expected(c)
    assert (exp.status, exp.aaa_reason) == ("REJECTED", reason)
