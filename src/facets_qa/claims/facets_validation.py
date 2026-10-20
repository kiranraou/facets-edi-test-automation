"""Facets backend validation: expected rows from the claim model vs. actual Facets rows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from facets_qa.claims.models import InstitutionalClaim, ProfessionalClaim


class FacetsRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ClclClaimRow(FacetsRow):
    CLCL_ID: str
    CLCL_PA_ACCT_NO: str
    CLCL_CL_TYPE: str
    CLCL_CL_SUB_TYPE: str
    CLCL_CUR_STS: str
    MEME_CK: int
    PRPR_ID: str
    CLCL_TOT_CHG: Decimal
    CLCL_LOW_SVC_DT: date
    CLCL_HIGH_SVC_DT: date
    PSCD_ID: Optional[str] = None


class CdmlLineRow(FacetsRow):
    CLCL_ID: str
    CDML_SEQ_NO: int
    IPCD_ID: Optional[str] = None
    CDML_MOD1: Optional[str] = None
    CDML_MOD2: Optional[str] = None
    CDML_MOD3: Optional[str] = None
    CDML_MOD4: Optional[str] = None
    RCRC_ID: Optional[str] = None
    CDML_CHG_AMT: Decimal
    CDML_UNITS: Decimal
    CDML_FROM_DT: date
    CDML_TO_DT: date


class ClhpHospRow(FacetsRow):
    CLCL_ID: str
    CLHP_FAC_TYPE: str
    CLHP_BILL_CLASS: str
    CLHP_FREQUENCY: str
    CLHP_ADM_DT: Optional[date] = None
    CLHP_DC_DT: Optional[date] = None
    CLHP_ADM_TYP: Optional[str] = None
    CLHP_DC_STAT: Optional[str] = None


@dataclass(frozen=True)
class Mismatch:
    table: str
    key: str
    column: str
    expected: Any
    actual: Any

    def __str__(self) -> str:
        return f"{self.table}[{self.key}].{self.column}: expected {self.expected!r}, actual {self.actual!r}"


# --------------------------------------------------------------------- expected values
def expected_header(claim, meme_ck: int, prpr_id: str) -> dict:
    low, high = claim.service_dates
    return {
        "CLCL_PA_ACCT_NO": claim.claim_id,
        "CLCL_CL_TYPE": "M",
        "CLCL_CL_SUB_TYPE": "M" if isinstance(claim, ProfessionalClaim) else "H",
        "MEME_CK": meme_ck,
        "PRPR_ID": prpr_id,
        "CLCL_TOT_CHG": claim.total_charge,
        "CLCL_LOW_SVC_DT": low,
        "CLCL_HIGH_SVC_DT": high,
        "PSCD_ID": claim.place_of_service if isinstance(claim, ProfessionalClaim) else None,
    }


def expected_lines(claim) -> dict[int, dict]:
    out = {}
    for ln in claim.lines:
        mods = list(ln.modifiers) + [None] * (4 - len(ln.modifiers))
        out[ln.line_number] = {
            "IPCD_ID": ln.procedure_code,
            "CDML_MOD1": mods[0], "CDML_MOD2": mods[1], "CDML_MOD3": mods[2], "CDML_MOD4": mods[3],
            "RCRC_ID": getattr(ln, "revenue_code", None),
            "CDML_CHG_AMT": ln.charge,
            "CDML_UNITS": ln.units,
            "CDML_FROM_DT": ln.service_date,
            "CDML_TO_DT": ln.service_date,
        }
    return out


def expected_hosp(claim: InstitutionalClaim) -> dict:
    return {
        "CLHP_FAC_TYPE": claim.bill_type[1],
        "CLHP_BILL_CLASS": claim.bill_type[2],
        "CLHP_FREQUENCY": claim.bill_type[3],
        "CLHP_ADM_DT": claim.admission_date,
        "CLHP_DC_DT": claim.discharge_date,
        "CLHP_ADM_TYP": claim.admission_type,
        "CLHP_DC_STAT": claim.patient_status,
    }


# --------------------------------------------------------------------- compare
def _compare(table: str, key: str, expected: dict, actual: BaseModel) -> list[Mismatch]:
    return [Mismatch(table, key, col, exp, getattr(actual, col))
            for col, exp in expected.items() if getattr(actual, col) != exp]


def fetch_rows(conn: sqlite3.Connection, clcl_id: str):
    hdr = conn.execute("SELECT * FROM CMC_CLCL_CLAIM WHERE CLCL_ID = ?", (clcl_id,)).fetchone()
    lines = conn.execute("SELECT * FROM CMC_CDML_CL_LINE WHERE CLCL_ID = ? ORDER BY CDML_SEQ_NO",
                         (clcl_id,)).fetchall()
    hosp = conn.execute("SELECT * FROM CMC_CLHP_HOSP WHERE CLCL_ID = ?", (clcl_id,)).fetchone()
    return (ClclClaimRow(**dict(hdr)) if hdr else None,
            [CdmlLineRow(**dict(r)) for r in lines],
            ClhpHospRow(**dict(hosp)) if hosp else None)


def validate_claim(conn: sqlite3.Connection, claim, clcl_id: str, meme_ck: int, prpr_id: str) -> list[Mismatch]:
    header, lines, hosp = fetch_rows(conn, clcl_id)
    if header is None:
        return [Mismatch("CMC_CLCL_CLAIM", clcl_id, "*", "row present", None)]
    out = _compare("CMC_CLCL_CLAIM", clcl_id, expected_header(claim, meme_ck, prpr_id), header)

    exp_lines = expected_lines(claim)
    act_lines = {ln.CDML_SEQ_NO: ln for ln in lines}
    for seq in sorted(set(exp_lines) | set(act_lines)):
        key = f"{clcl_id}/{seq}"
        if seq not in act_lines:
            out.append(Mismatch("CMC_CDML_CL_LINE", key, "*", "row present", None))
        elif seq not in exp_lines:
            out.append(Mismatch("CMC_CDML_CL_LINE", key, "*", None, "unexpected row"))
        else:
            out += _compare("CMC_CDML_CL_LINE", key, exp_lines[seq], act_lines[seq])

    if isinstance(claim, InstitutionalClaim):
        if hosp is None:
            out.append(Mismatch("CMC_CLHP_HOSP", clcl_id, "*", "row present", None))
        else:
            out += _compare("CMC_CLHP_HOSP", clcl_id, expected_hosp(claim), hosp)
    return out
