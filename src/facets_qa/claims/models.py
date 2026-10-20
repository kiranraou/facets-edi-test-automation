"""Pydantic models for 837P / 837I claims with business rules as validators.

``Claim`` is a discriminated union on ``claim_type`` so professional and
institutional claims flow through one interface (``ClaimAdapter``).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from facets_qa.common.npi import is_valid_npi

ICD10_RE = re.compile(r"^[A-Z][0-9][0-9A-Z](\.?[0-9A-Z]{1,4})?$")
PROCEDURE_RE = r"^([0-9]{4}[0-9A-Z]|[A-Z][0-9]{4})$"  # CPT or HCPCS level II
MODIFIER_RE = r"^[0-9A-Z]{2}$"
REVENUE_RE = r"^[0-9]{4}$"
BILL_TYPE_RE = r"^0[1-8][1-9][0-9A-Z]$"  # leading zero + facility type + classification + frequency
PLACES_OF_SERVICE = frozenset({"02", "10", "11", "12", "19", "20", "21", "22", "23", "31", "32", "81"})
INPATIENT_FACILITY_TYPES = frozenset({"11", "18", "21", "41"})  # bill type digits 2-3

Money = Annotated[Decimal, Field(gt=0, max_digits=11, decimal_places=2)]
Units = Annotated[Decimal, Field(gt=0, max_digits=9, decimal_places=3)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


# --------------------------------------------------------------------- parties
class Address(StrictModel):
    line1: str = Field(min_length=1, max_length=55)
    city: str = Field(min_length=1, max_length=30)
    state: str = Field(pattern=r"^[A-Z]{2}$")
    zip: str = Field(pattern=r"^\d{5}(\d{4})?$")


class BillingProvider(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    npi: str
    tax_id: str = Field(pattern=r"^\d{9}$")
    address: Address

    @field_validator("npi")
    @classmethod
    def _npi(cls, v: str) -> str:
        if not is_valid_npi(v):
            raise ValueError(f"NPI {v!r} fails the check-digit (Luhn) validation")
        return v

    @field_validator("tax_id")
    @classmethod
    def _tax_id(cls, v: str) -> str:
        if v[:2] in {"00", "07", "08", "09", "17", "18", "19", "28", "29", "49", "69", "70", "78", "79", "89"}:
            raise ValueError(f"Tax ID prefix {v[:2]} is not a valid EIN campus prefix")
        return v


class Subscriber(StrictModel):
    member_id: str = Field(pattern=r"^[A-Z0-9]{2,20}$")
    first_name: str = Field(min_length=1, max_length=35)
    last_name: str = Field(min_length=1, max_length=60)
    dob: date
    gender: Literal["M", "F", "U"]
    address: Address


class Payer(StrictModel):
    payer_id: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=1, max_length=60)


# --------------------------------------------------------------------- service lines
class ServiceLine(StrictModel):
    line_number: int = Field(ge=1)
    charge: Money
    units: Units = Decimal("1")
    service_date: date
    procedure_code: Optional[str] = Field(default=None, pattern=PROCEDURE_RE)
    modifiers: tuple[Annotated[str, Field(pattern=MODIFIER_RE)], ...] = ()

    @field_validator("modifiers")
    @classmethod
    def _max_modifiers(cls, v):
        if len(v) > 4:
            raise ValueError(f"A service line allows at most 4 modifiers, got {len(v)}")
        return v


class ProfessionalLine(ServiceLine):
    place_of_service: Optional[str] = None
    diagnosis_pointers: tuple[Annotated[int, Field(ge=1, le=12)], ...] = (1,)

    @model_validator(mode="after")
    def _procedure_required(self):
        if not self.procedure_code:
            raise ValueError(f"837P line {self.line_number} requires a procedure code (SV101)")
        if not 1 <= len(self.diagnosis_pointers) <= 4:
            raise ValueError("837P lines carry 1-4 diagnosis pointers (SV107)")
        return self


class InstitutionalLine(ServiceLine):
    revenue_code: Optional[str] = Field(default=None, pattern=REVENUE_RE)

    @model_validator(mode="after")
    def _revenue_required(self):
        if not self.revenue_code:
            raise ValueError(f"837I line {self.line_number} requires a revenue code (SV201)")
        return self


# --------------------------------------------------------------------- claims
class ClaimBase(StrictModel):
    claim_id: str = Field(pattern=r"^[A-Za-z0-9\-]{1,20}$")
    billing_provider: BillingProvider
    subscriber: Subscriber
    payer: Payer
    total_charge: Money
    diagnosis_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("diagnosis_codes")
    @classmethod
    def _icd10(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) > 12:
            raise ValueError(f"At most 12 diagnosis codes are allowed, got {len(v)}")
        bad = [c for c in v if not ICD10_RE.match(c)]
        if bad:
            raise ValueError(f"Invalid ICD-10-CM diagnosis code(s): {bad}")
        return tuple(c.replace(".", "") for c in v)

    @model_validator(mode="after")
    def _claim_rules(self):
        lines = self.lines  # defined on subclasses
        if not lines:
            raise ValueError("Claim must have at least one service line")
        numbers = [ln.line_number for ln in lines]
        if numbers != list(range(1, len(lines) + 1)):
            raise ValueError(f"Service lines must be numbered sequentially from 1, got {numbers}")
        line_total = sum((ln.charge for ln in lines), Decimal("0"))
        if line_total != self.total_charge:
            raise ValueError(f"Total charge {self.total_charge} does not equal sum of line charges {line_total}")
        today = date.today()
        for ln in lines:
            if ln.service_date > today:
                raise ValueError(f"Line {ln.line_number} service date {ln.service_date} is in the future")
            if ln.service_date < self.subscriber.dob:
                raise ValueError(f"Line {ln.line_number} service date {ln.service_date} is before patient DOB")
        return self

    @property
    def service_dates(self) -> tuple[date, date]:
        dates = [ln.service_date for ln in self.lines]
        return min(dates), max(dates)


class ProfessionalClaim(ClaimBase):
    claim_type: Literal["837P"] = "837P"
    place_of_service: str
    frequency_code: Literal["1", "7", "8"] = "1"
    lines: tuple[ProfessionalLine, ...] = Field(min_length=1, max_length=50)

    @field_validator("place_of_service")
    @classmethod
    def _pos(cls, v: str) -> str:
        if v not in PLACES_OF_SERVICE:
            raise ValueError(f"Unsupported place of service {v!r}")
        return v

    @model_validator(mode="after")
    def _pointers(self):
        for ln in self.lines:
            if max(ln.diagnosis_pointers) > len(self.diagnosis_codes):
                raise ValueError(f"Line {ln.line_number} points to a diagnosis that does not exist")
        return self


class InstitutionalClaim(ClaimBase):
    claim_type: Literal["837I"] = "837I"
    bill_type: str = Field(pattern=BILL_TYPE_RE)
    statement_from: date
    statement_to: date
    admission_date: Optional[date] = None
    discharge_date: Optional[date] = None
    admission_type: Optional[Literal["1", "2", "3", "4", "5", "9"]] = None
    admission_source: Optional[str] = Field(default=None, pattern=r"^[0-9A-Z]$")
    patient_status: str = Field(pattern=r"^\d{2}$")
    lines: tuple[InstitutionalLine, ...] = Field(min_length=1, max_length=999)

    @property
    def is_inpatient(self) -> bool:
        return self.bill_type[1:3] in INPATIENT_FACILITY_TYPES

    @model_validator(mode="after")
    def _institutional_dates(self):
        if self.statement_to < self.statement_from:
            raise ValueError("Statement through date is before statement from date")
        if self.is_inpatient:
            if self.admission_date is None:
                raise ValueError(f"Inpatient bill type {self.bill_type} requires an admission date (DTP*435)")
            if self.admission_type is None:
                raise ValueError(f"Inpatient bill type {self.bill_type} requires an admission type (CL101)")
        if self.admission_date and self.admission_date > self.statement_from:
            raise ValueError("Admission date is after the statement from date")
        if self.discharge_date:
            if self.admission_date and self.discharge_date < self.admission_date:
                raise ValueError("Discharge date is before admission date")
            if self.discharge_date != self.statement_to:
                raise ValueError("Discharge date must equal the statement through date")
        for ln in self.lines:
            if not self.statement_from <= ln.service_date <= self.statement_to:
                raise ValueError(f"Line {ln.line_number} service date is outside the statement period")
        return self

    @property
    def service_dates(self) -> tuple[date, date]:
        return self.statement_from, self.statement_to


Claim = Annotated[Union[ProfessionalClaim, InstitutionalClaim], Field(discriminator="claim_type")]
ClaimAdapter: TypeAdapter[Claim] = TypeAdapter(Claim)
