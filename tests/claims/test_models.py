from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from facets_qa.claims.generator import SyntheticFactory
from facets_qa.claims.models import ClaimAdapter, InstitutionalClaim, ProfessionalClaim
from facets_qa.claims.scenarios import SCENARIOS, by_name
from facets_qa.common.npi import is_valid_npi

pytestmark = pytest.mark.claims
DOS = date.today() - timedelta(days=14)

POSITIVE = [s for s in SCENARIOS if s.polarity == "positive"]
NEGATIVE = [s for s in SCENARIOS if s.polarity == "negative"]


@pytest.mark.parametrize("sc", POSITIVE, ids=lambda s: s.name)
def test_positive_scenarios_validate(sc, factory):
    claim = ClaimAdapter.validate_python(sc.build(factory, DOS))
    assert claim.claim_type == sc.claim_type
    assert isinstance(claim, ProfessionalClaim if sc.claim_type == "837P" else InstitutionalClaim)


@pytest.mark.parametrize("sc", NEGATIVE, ids=lambda s: s.name)
def test_negative_scenarios_are_rejected_with_the_right_rule(sc, factory):
    with pytest.raises(ValidationError) as exc:
        ClaimAdapter.validate_python(sc.build(factory, DOS))
    assert sc.expected_error in str(exc.value)


def test_discriminator_routes_by_claim_type(factory):
    payload = by_name("office_visit_established").build(factory, DOS)
    payload["claim_type"] = "837X"
    with pytest.raises(ValidationError, match="union_tag_invalid|does not match any of the expected tags"):
        ClaimAdapter.validate_python(payload)


def test_unknown_fields_rejected(factory):
    payload = by_name("office_visit_established").build(factory, DOS)
    payload["ssn"] = "000-00-0000"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ClaimAdapter.validate_python(payload)


def _office(factory, **overrides):
    payload = by_name("office_visit_established").build(factory, DOS)
    for path, value in overrides.items():
        target = payload
        *parents, leaf = path.split(".")
        for p in parents:
            target = target[int(p)] if p.isdigit() else target[p]
        target[leaf] = value
    return payload


@pytest.mark.parametrize("path,value,message", [
    ("billing_provider.npi", "1234567890", "check-digit"),
    ("billing_provider.tax_id", "001234567", "EIN campus prefix"),
    ("billing_provider.tax_id", "12345", "String should match pattern"),
    ("place_of_service", "99", "Unsupported place of service"),
    ("lines.0.procedure_code", None, "requires a procedure code"),
    ("lines.0.procedure_code", "9921", "String should match pattern"),
    ("lines.0.charge", Decimal("0"), "greater than 0"),
    ("lines.0.charge", Decimal("10.001"), "decimal places"),
    ("lines.0.diagnosis_pointers", (2,), "points to a diagnosis that does not exist"),
    ("lines.0.modifiers", ("2",), "String should match pattern"),
    ("subscriber.dob", DOS + timedelta(days=1), "before patient DOB"),
])
def test_professional_rules(factory, path, value, message):
    with pytest.raises(ValidationError, match=message):
        ClaimAdapter.validate_python(_office(factory, **{path: value}))


@pytest.mark.parametrize("code,ok", [("E11.9", True), ("E119", True), ("M54.50", True), ("S72.001A", True),
                                     ("U07.1", True), ("123.4", False), ("E11.12345", False), ("E1", False), ("e11.9", False)])
def test_icd10_format(factory, code, ok):
    payload = _office(factory, diagnosis_codes=[code])
    if ok:
        assert ClaimAdapter.validate_python(payload).diagnosis_codes == (code.replace(".", ""),)
    else:
        with pytest.raises(ValidationError, match="ICD-10"):
            ClaimAdapter.validate_python(payload)


def test_exactly_twelve_diagnoses_and_four_modifiers_allowed(factory):
    payload = _office(factory, diagnosis_codes=[f"E11.{i}" for i in range(10)] + ["I10", "E78.5"])
    payload["lines"][0]["modifiers"] = ("25", "59", "XU", "76")
    claim = ClaimAdapter.validate_python(payload)
    assert len(claim.diagnosis_codes) == 12 and len(claim.lines[0].modifiers) == 4


# --------------------------------------------------------------------- 837I specific
def _inpatient(factory):
    return by_name("inpatient_0111").build(factory, DOS)


@pytest.mark.parametrize("field,value,message", [
    ("bill_type", "111", "String should match pattern"),
    ("bill_type", "0011", "String should match pattern"),
    ("admission_type", None, "requires an admission type"),
    ("discharge_date", DOS - timedelta(days=10), "before admission date"),
    ("discharge_date", DOS - timedelta(days=1), "must equal the statement through date"),
    ("admission_date", DOS, "after the statement from date"),
    ("statement_to", DOS - timedelta(days=5), "Statement through date is before"),
])
def test_institutional_rules(factory, field, value, message):
    payload = _inpatient(factory)
    payload[field] = value
    with pytest.raises(ValidationError, match=message):
        ClaimAdapter.validate_python(payload)


def test_outpatient_does_not_need_admission_date(factory):
    claim = ClaimAdapter.validate_python(by_name("outpatient_0131").build(factory, DOS))
    assert not claim.is_inpatient and claim.admission_date is None


def test_line_outside_statement_period(factory):
    payload = _inpatient(factory)
    payload["lines"][0]["service_date"] = payload["statement_from"] - timedelta(days=1)
    with pytest.raises(ValidationError, match="outside the statement period"):
        ClaimAdapter.validate_python(payload)


# --------------------------------------------------------------------- synthetic data
def test_factory_is_deterministic_and_phi_safe():
    a, b = SyntheticFactory(5), SyntheticFactory(5)
    assert [a.subscriber() for _ in range(5)] == [b.subscriber() for _ in range(5)]
    p = SyntheticFactory(5).provider()
    assert is_valid_npi(p["npi"]) and len(p["tax_id"]) == 9
    sub = SyntheticFactory(9).subscriber()
    assert sub["member_id"].startswith("7") and "TEST DATA" in sub["address"]["line1"]
