"""Facets is the source of truth: derive the expected 271 outcome from CMC_MEPE_PRCS_ELIG."""

from __future__ import annotations

from datetime import date
from typing import Sequence

from facets_qa.eligibility.models import EligibilitySpan, ExpectedEligibility, FacetsCandidate, Scenario


def classify_scenario(spans: Sequence[EligibilitySpan], service_date: date) -> Scenario:
    medical = [s for s in spans if s.cspd_cat == "M"]
    if not medical:
        return Scenario.NO_ELIGIBILITY
    covering = [s for s in medical if s.covers(service_date)]
    if any(s.elig_ind == "Y" for s in covering):
        prior_y = [s for s in medical if s.elig_ind == "Y" and s.term_dt < service_date]
        return Scenario.REINSTATED if prior_y else Scenario.ACTIVE
    if covering:  # a span covers the date but the eligibility indicator is off
        return Scenario.ELIG_IND_OFF
    past = [s for s in medical if s.elig_ind == "Y" and s.term_dt < service_date]
    future = [s for s in medical if s.elig_ind == "Y" and s.eff_dt > service_date]
    if past and future:
        return Scenario.COVERAGE_GAP
    if future:
        return Scenario.FUTURE_EFFECTIVE
    if past:
        return Scenario.TERMINATED
    return Scenario.ELIG_IND_OFF


def derive_expected(candidate: FacetsCandidate) -> ExpectedEligibility:
    if candidate.probe == "invalid_member_id":
        return ExpectedEligibility(status="REJECTED", scenario=Scenario.INVALID_MEMBER_ID, aaa_reason="72")
    if candidate.probe == "member_not_found" or not candidate.in_facets:
        return ExpectedEligibility(status="REJECTED", scenario=Scenario.MEMBER_NOT_FOUND, aaa_reason="75")

    svc = candidate.service_date
    scenario = classify_scenario(candidate.spans, svc)
    active = next((s for s in candidate.spans
                   if s.cspd_cat == "M" and s.elig_ind == "Y" and s.covers(svc)), None)
    if active:
        return ExpectedEligibility(status="ACTIVE", scenario=scenario,
                                   coverage_start=active.eff_dt, coverage_end=active.term_dt)
    return ExpectedEligibility(status="INACTIVE", scenario=scenario)
