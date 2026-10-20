"""Pydantic contracts for Facets candidates, 270 requests and 271 responses."""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from facets_qa.common.npi import is_valid_npi

# X12 service type codes accepted by our 270 builder (subset of the 5010 code list).
SERVICE_TYPE_CODES = frozenset({
    "1", "30", "33", "35", "47", "48", "50", "86", "88", "98", "A4", "AL", "MH", "UC",
})

# 271 AAA03 reject reasons we assert on.
AAA_REASONS = {
    "42": "Unable to respond at current time",
    "58": "Invalid/missing date of birth",
    "71": "Patient birth date does not match that for the patient on the database",
    "72": "Invalid/missing subscriber/insured ID",
    "73": "Invalid/missing subscriber/insured name",
    "75": "Subscriber/insured not found",
}

MAX_FUTURE_SERVICE_DAYS = 365
MAX_PAST_SERVICE_DAYS = 365 * 2


class StrictModel(BaseModel):
    """Rejects unknown fields so schema drift in source data fails loudly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class Scenario(str, Enum):
    ACTIVE = "active"
    REINSTATED = "reinstated"
    TERMINATED = "terminated"
    FUTURE_EFFECTIVE = "future_effective"
    ELIG_IND_OFF = "elig_ind_off"
    COVERAGE_GAP = "coverage_gap"
    NO_ELIGIBILITY = "no_eligibility"
    MEMBER_NOT_FOUND = "member_not_found"
    INVALID_MEMBER_ID = "invalid_member_id"


CoverageStatus = Literal["ACTIVE", "INACTIVE", "REJECTED", "UNKNOWN"]


# --------------------------------------------------------------------- Facets side
class EligibilitySpan(StrictModel):
    """One CMC_MEPE_PRCS_ELIG row."""

    eff_dt: date
    term_dt: date
    elig_ind: Literal["Y", "N"]
    cspd_cat: str = "M"
    pdpd_id: Optional[str] = None

    @model_validator(mode="after")
    def _term_not_before_eff(self):
        if self.term_dt < self.eff_dt:
            raise ValueError(f"MEPE_TERM_DT {self.term_dt} is before MEPE_EFF_DT {self.eff_dt}")
        return self

    def covers(self, d: date) -> bool:
        return self.eff_dt <= d <= self.term_dt


class FacetsCandidate(StrictModel):
    """A member selected from Facets (or a synthetic negative probe) to be eligibility-checked."""

    meme_ck: Optional[int] = Field(default=None, gt=0)
    member_id: str = Field(min_length=1, max_length=80)
    first_name: str = Field(min_length=1, max_length=35)
    last_name: str = Field(min_length=1, max_length=60)
    dob: date
    gender: Literal["M", "F", "U"] = "U"
    service_date: date
    in_facets: bool = True
    probe: Optional[Literal["member_not_found", "invalid_member_id"]] = None
    spans: tuple[EligibilitySpan, ...] = ()

    @model_validator(mode="after")
    def _consistency(self):
        if self.dob > self.service_date:
            raise ValueError("Member DOB is after the service date")
        if self.in_facets and self.meme_ck is None:
            raise ValueError("Facets members must carry MEME_CK")
        if self.probe and self.in_facets:
            raise ValueError("Negative probes must not reference a real Facets member")
        return self


class ExpectedEligibility(StrictModel):
    status: Literal["ACTIVE", "INACTIVE", "REJECTED"]
    scenario: Scenario
    aaa_reason: Optional[str] = None
    coverage_start: Optional[date] = None
    coverage_end: Optional[date] = None


# --------------------------------------------------------------------- 270 request
class Eligibility270Request(StrictModel):
    trace_number: str = Field(pattern=r"^[A-Za-z0-9\-]{1,50}$")
    payer_id: str = Field(min_length=2, max_length=80)
    payer_name: str = Field(min_length=1, max_length=60)
    provider_npi: str
    provider_name: str = Field(min_length=1, max_length=60)
    member_id: str = Field(pattern=r"^[A-Za-z0-9\-]{1,80}$")
    member_first: str = Field(min_length=1, max_length=35)
    member_last: str = Field(min_length=1, max_length=60)
    member_dob: date
    service_date: date
    service_type_codes: tuple[str, ...] = ("30",)

    @field_validator("provider_npi")
    @classmethod
    def _npi(cls, v: str) -> str:
        if not is_valid_npi(v):
            raise ValueError(f"NPI {v!r} fails the check-digit (Luhn) validation")
        return v

    @field_validator("service_type_codes")
    @classmethod
    def _stc(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("At least one service type code is required")
        if len(v) > 99:
            raise ValueError("EQ01 allows at most 99 repetitions")
        unknown = [c for c in v if c not in SERVICE_TYPE_CODES]
        if unknown:
            raise ValueError(f"Unknown service type code(s): {unknown}")
        return v

    @model_validator(mode="after")
    def _dates(self):
        today = date.today()
        if self.member_dob > today:
            raise ValueError("Member DOB cannot be in the future")
        if self.member_dob > self.service_date:
            raise ValueError("Service date cannot be before member DOB")
        if self.service_date > today + timedelta(days=MAX_FUTURE_SERVICE_DAYS):
            raise ValueError("Service date is more than 12 months in the future")
        if self.service_date < today - timedelta(days=MAX_PAST_SERVICE_DAYS):
            raise ValueError("Service date is more than 24 months in the past")
        return self


# --------------------------------------------------------------------- 271 response
class AAARejection(StrictModel):
    loop: Literal["2100A", "2100B", "2100C", "2100D"]
    valid_request: Literal["Y", "N"]
    reject_reason: str = Field(pattern=r"^[0-9A-Z]{2}$")
    follow_up_action: Optional[str] = None

    @property
    def description(self) -> str:
        return AAA_REASONS.get(self.reject_reason, "Unknown reject reason")


class BenefitLoop(StrictModel):
    """One EB segment (loop 2110C) plus its DTP dates."""

    eligibility_code: str = Field(pattern=r"^[0-9A-Z]{1,2}$")
    coverage_level: Optional[str] = None
    service_type_codes: tuple[str, ...] = ()
    insurance_type: Optional[str] = None
    plan_description: Optional[str] = None
    coverage_start: Optional[date] = None
    coverage_end: Optional[date] = None

    @model_validator(mode="after")
    def _dates(self):
        if self.coverage_start and self.coverage_end and self.coverage_end < self.coverage_start:
            raise ValueError("EB coverage end date is before start date")
        return self


class Eligibility271Response(StrictModel):
    trace_number: Optional[str] = None
    payer_id: Optional[str] = None
    member_id: Optional[str] = None
    member_first: Optional[str] = None
    member_last: Optional[str] = None
    member_dob: Optional[date] = None
    plan_begin: Optional[date] = None
    plan_end: Optional[date] = None
    benefits: tuple[BenefitLoop, ...] = ()
    rejections: tuple[AAARejection, ...] = ()

    @model_validator(mode="after")
    def _has_content(self):
        if not self.benefits and not self.rejections:
            raise ValueError("271 contains neither EB benefit loops nor AAA rejections")
        return self

    @property
    def coverage_status(self) -> CoverageStatus:
        if self.rejections:
            return "REJECTED"
        codes = {b.eligibility_code for b in self.benefits}
        if "1" in codes:
            return "ACTIVE"
        if "6" in codes:
            return "INACTIVE"
        return "UNKNOWN"

    @property
    def primary_reject_reason(self) -> Optional[str]:
        return self.rejections[0].reject_reason if self.rejections else None

    def active_benefit(self) -> Optional[BenefitLoop]:
        return next((b for b in self.benefits if b.eligibility_code == "1"), None)
