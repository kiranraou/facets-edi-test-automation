"""Scenario-driven claims test catalog (positive and negative, 837P and 837I)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Literal, Optional

from facets_qa.claims.generator import SyntheticFactory


@dataclass(frozen=True)
class ClaimScenario:
    name: str
    claim_type: Literal["837P", "837I"]
    polarity: Literal["positive", "negative"]
    description: str
    build: Callable[[SyntheticFactory, date], dict]
    expected_error: Optional[str] = None  # substring the validation error must contain


def _p_line(n: int, code: str, charge: str, dos: date, units: str = "1", mods: tuple = (), ptrs: tuple = (1,)) -> dict:
    return {"line_number": n, "procedure_code": code, "modifiers": mods, "charge": Decimal(charge),
            "units": Decimal(units), "service_date": dos, "diagnosis_pointers": ptrs}


def _i_line(n: int, rev: Optional[str], charge: str, dos: date, code: Optional[str] = None, units: str = "1") -> dict:
    line = {"line_number": n, "charge": Decimal(charge), "units": Decimal(units), "service_date": dos,
            "procedure_code": code}
    if rev is not None:
        line["revenue_code"] = rev
    return line


def professional(f: SyntheticFactory, claim_id: str, lines: list[dict], dx: list[str], pos: str = "11",
                 total: Optional[Decimal] = None) -> dict:
    return {
        "claim_type": "837P", "claim_id": claim_id, "place_of_service": pos,
        "billing_provider": f.provider(), "subscriber": f.subscriber(), "payer": f.payer(),
        "diagnosis_codes": dx, "lines": lines,
        "total_charge": total if total is not None else sum((ln["charge"] for ln in lines), Decimal("0")),
    }


def institutional(f: SyntheticFactory, claim_id: str, bill_type: str, lines: list[dict], dx: list[str],
                  stmt_from: date, stmt_to: date, **extra) -> dict:
    return {
        "claim_type": "837I", "claim_id": claim_id, "bill_type": bill_type,
        "billing_provider": f.provider(), "subscriber": f.subscriber(), "payer": f.payer(),
        "diagnosis_codes": dx, "lines": lines, "statement_from": stmt_from, "statement_to": stmt_to,
        "patient_status": extra.pop("patient_status", "01"),
        "total_charge": sum((ln["charge"] for ln in lines), Decimal("0")), **extra,
    }


def _inpatient(f: SyntheticFactory, dos: date, claim_id: str, with_admission: bool = True) -> dict:
    adm, dc = dos - timedelta(days=3), dos
    extra = {"admission_type": "1", "admission_source": "7", "discharge_date": dc}
    if with_admission:
        extra["admission_date"] = adm
    lines = [_i_line(1, "0120", "3600.00", adm, units="3"),
             _i_line(2, "0250", "412.50", adm + timedelta(days=1)),
             _i_line(3, "0300", "275.00", adm + timedelta(days=1), code="80053")]
    return institutional(f, claim_id, "0111", lines, ["I21.4", "I10", "E78.5"], adm, dc, **extra)


SCENARIOS: list[ClaimScenario] = [
    # ------------------------------------------------------------ positive 837P
    ClaimScenario("office_visit_established", "837P", "positive", "Established patient E/M visit",
                  lambda f, d: professional(f, "P-OV-001", [_p_line(1, "99213", "125.00", d)], ["J06.9"])),
    ClaimScenario("office_visit_modifier_25", "837P", "positive",
                  "Significant, separately identifiable E/M with same-day procedure (modifier 25)",
                  lambda f, d: professional(f, "P-MOD25-001", [
                      _p_line(1, "99214", "180.00", d, mods=("25",), ptrs=(1, 2)),
                      _p_line(2, "93000", "45.00", d, ptrs=(2,)),
                  ], ["E11.9", "R00.2"])),
    ClaimScenario("telehealth_pos_10", "837P", "positive", "Telehealth in patient's home (POS 10, modifier 95)",
                  lambda f, d: professional(f, "P-TELE-001", [_p_line(1, "99213", "110.00", d, mods=("95",))],
                                            ["F41.1"], pos="10")),
    ClaimScenario("multi_unit_therapy", "837P", "positive", "Therapeutic exercise billed in 15-minute units",
                  lambda f, d: professional(f, "P-UNIT-001", [
                      _p_line(1, "97110", "240.00", d, units="4", mods=("GP",)),
                      _p_line(2, "97140", "65.00", d, mods=("GP", "59")),
                  ], ["M54.50"])),
    # ------------------------------------------------------------ positive 837I
    ClaimScenario("inpatient_0111", "837I", "positive", "Inpatient admit-through-discharge (bill type 0111)",
                  lambda f, d: _inpatient(f, d, "I-INP-001")),
    ClaimScenario("outpatient_0131", "837I", "positive", "Hospital outpatient ED visit (bill type 0131)",
                  lambda f, d: institutional(f, "I-OUT-001", "0131", [
                      _i_line(1, "0450", "850.00", d, code="99284"),
                      _i_line(2, "0320", "310.00", d, code="71046"),
                  ], ["R07.9"], d, d)),
    # ------------------------------------------------------------ negative
    ClaimScenario("invalid_diagnosis_code", "837P", "negative", "Non ICD-10 diagnosis code",
                  lambda f, d: professional(f, "N-DX-001", [_p_line(1, "99213", "125.00", d)], ["123.45"]),
                  "Invalid ICD-10-CM"),
    ClaimScenario("future_date_of_service", "837P", "negative", "Service date after today",
                  lambda f, d: professional(f, "N-FUT-001",
                                            [_p_line(1, "99213", "125.00", date.today() + timedelta(days=10))],
                                            ["J06.9"]),
                  "in the future"),
    ClaimScenario("total_charge_mismatch", "837P", "negative", "CLM02 differs from sum of SV102",
                  lambda f, d: professional(f, "N-TOT-001", [_p_line(1, "99213", "125.00", d)], ["J06.9"],
                                            total=Decimal("150.00")),
                  "does not equal sum of line charges"),
    ClaimScenario("too_many_modifiers", "837P", "negative", "Five modifiers on one line",
                  lambda f, d: professional(f, "N-MOD-001", [_p_line(1, "97110", "60.00", d,
                                                                     mods=("GP", "59", "KX", "XU", "76"))],
                                            ["M54.50"]),
                  "at most 4 modifiers"),
    ClaimScenario("too_many_diagnoses", "837P", "negative", "Thirteen diagnosis codes",
                  lambda f, d: professional(f, "N-DX13-001", [_p_line(1, "99215", "250.00", d)],
                                            [f"E11.{i}" for i in range(10)] + ["I10", "E78.5", "N18.3"]),
                  "At most 12 diagnosis codes"),
    ClaimScenario("non_sequential_lines", "837P", "negative", "Line numbers skip from 1 to 3",
                  lambda f, d: professional(f, "N-SEQ-001", [_p_line(1, "99213", "125.00", d),
                                                             _p_line(3, "93000", "45.00", d)], ["J06.9"]),
                  "numbered sequentially"),
    ClaimScenario("missing_revenue_code", "837I", "negative", "Institutional line without SV201",
                  lambda f, d: institutional(f, "N-REV-001", "0131", [_i_line(1, None, "850.00", d, code="99284")],
                                             ["R07.9"], d, d),
                  "requires a revenue code"),
    ClaimScenario("inpatient_without_admission_date", "837I", "negative", "Bill type 0111 without DTP*435",
                  lambda f, d: _inpatient(f, d, "N-ADM-001", with_admission=False),
                  "requires an admission date"),
]


def by_name(name: str) -> ClaimScenario:
    return next(s for s in SCENARIOS if s.name == name)
