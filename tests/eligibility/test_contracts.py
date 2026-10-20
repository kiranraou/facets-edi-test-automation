from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from facets_qa.eligibility.models import (
    AAARejection, BenefitLoop, Eligibility270Request, Eligibility271Response, EligibilitySpan, FacetsCandidate,
)

pytestmark = pytest.mark.contracts


def test_valid_candidate(candidate_row):
    c = FacetsCandidate(**candidate_row)
    assert c.spans[0].elig_ind == "Y"


def test_candidate_rejects_unknown_fields(candidate_row):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FacetsCandidate(**candidate_row, MEME_SSN="000000000")


def test_candidate_dob_after_service_date(candidate_row):
    candidate_row["dob"] = candidate_row["service_date"] + timedelta(days=1)
    with pytest.raises(ValidationError, match="DOB is after the service date"):
        FacetsCandidate(**candidate_row)


def test_facets_member_requires_meme_ck(candidate_row):
    candidate_row["meme_ck"] = None
    with pytest.raises(ValidationError, match="MEME_CK"):
        FacetsCandidate(**candidate_row)


def test_span_term_before_eff_rejected():
    with pytest.raises(ValidationError, match="before MEPE_EFF_DT"):
        EligibilitySpan(eff_dt=date(2025, 6, 1), term_dt=date(2025, 1, 1), elig_ind="Y")


@pytest.mark.parametrize("ind", ["X", "", "y"])
def test_span_elig_ind_must_be_y_or_n(ind):
    with pytest.raises(ValidationError):
        EligibilitySpan(eff_dt=date(2025, 1, 1), term_dt=date(2025, 12, 31), elig_ind=ind)


def test_valid_270_request(request_270_kwargs):
    assert Eligibility270Request(**request_270_kwargs).service_type_codes == ("30",)


@pytest.mark.parametrize("field,value,message", [
    ("provider_npi", "1234567890", "check-digit"),
    ("service_type_codes", ("30", "ZZ"), "Unknown service type code"),
    ("service_type_codes", (), "At least one service type code"),
    ("member_dob", date.today() + timedelta(days=1), "DOB cannot be in the future"),
    ("service_date", date.today() + timedelta(days=400), "more than 12 months in the future"),
    ("service_date", date.today() - timedelta(days=800), "more than 24 months in the past"),
    ("member_id", "BAD ID!", "String should match pattern"),
    ("trace_number", "", "String should match pattern"),
])
def test_270_request_rules(request_270_kwargs, field, value, message):
    request_270_kwargs[field] = value
    with pytest.raises(ValidationError, match=message):
        Eligibility270Request(**request_270_kwargs)


def test_270_service_date_before_dob(request_270_kwargs):
    request_270_kwargs["member_dob"] = request_270_kwargs["service_date"] + timedelta(days=1)
    with pytest.raises(ValidationError, match="before member DOB"):
        Eligibility270Request(**request_270_kwargs)


def test_270_rejects_unknown_fields(request_270_kwargs):
    with pytest.raises(ValidationError, match="Extra inputs"):
        Eligibility270Request(**request_270_kwargs, group_number="G1")


def test_271_requires_benefits_or_rejections():
    with pytest.raises(ValidationError, match="neither EB benefit loops nor AAA"):
        Eligibility271Response(trace_number="T1")


@pytest.mark.parametrize("codes,status", [(["1"], "ACTIVE"), (["6"], "INACTIVE"), (["1", "6"], "ACTIVE"),
                                          (["B"], "UNKNOWN")])
def test_271_coverage_status(codes, status):
    resp = Eligibility271Response(benefits=tuple(BenefitLoop(eligibility_code=c) for c in codes))
    assert resp.coverage_status == status


def test_271_rejection_wins():
    resp = Eligibility271Response(
        benefits=(BenefitLoop(eligibility_code="1"),),
        rejections=(AAARejection(loop="2100C", valid_request="N", reject_reason="72"),),
    )
    assert resp.coverage_status == "REJECTED"
    assert resp.rejections[0].description.startswith("Invalid/missing subscriber")


def test_eb_dates_must_be_ordered():
    with pytest.raises(ValidationError, match="end date is before start"):
        BenefitLoop(eligibility_code="1", coverage_start=date(2025, 2, 1), coverage_end=date(2025, 1, 1))
