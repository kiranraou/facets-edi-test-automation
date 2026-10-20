"""Write 5010 837P (005010X222A1) and 837I (005010X223A2) interchanges."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from facets_qa.claims.models import InstitutionalClaim, ProfessionalClaim
from facets_qa.common.dates import format_d8, format_rd8
from facets_qa.common.x12 import EnvelopeConfig, comp, seg, wrap_interchange

IMPL = {"837P": "005010X222A1", "837I": "005010X223A2"}
SUBMITTER = ("FACETS QA SUBMITTER", "FACETSQA")


def envelope(claim_type: str) -> EnvelopeConfig:
    return EnvelopeConfig(sender_id="FACETSQA", receiver_id="FACETS01", functional_id="HC",
                          implementation_ref=IMPL[claim_type])


def _amt(v: Decimal) -> str:
    """X12 R type: no trailing zeros beyond what's needed (125.00 -> 125, 412.50 -> 412.5)."""
    return format(v.normalize(), "f") if v != v.to_integral() else str(int(v))


def _header(claim, ts: datetime) -> list[str]:
    bp, sub, payer = claim.billing_provider, claim.subscriber, claim.payer
    return [
        seg("BHT", "0019", "00", claim.claim_id, ts.strftime("%Y%m%d"), ts.strftime("%H%M"), "CH"),
        seg("NM1", "41", 2, SUBMITTER[0], "", "", "", "", "46", SUBMITTER[1]),
        seg("PER", "IC", "QA AUTOMATION", "TE", "5555550100"),
        seg("NM1", "40", 2, payer.name, "", "", "", "", "46", payer.payer_id),
        # 2000A / 2010AA billing provider
        seg("HL", 1, "", 20, 1),
        seg("NM1", "85", 2, bp.name, "", "", "", "", "XX", bp.npi),
        seg("N3", bp.address.line1),
        seg("N4", bp.address.city, bp.address.state, bp.address.zip),
        seg("REF", "EI", bp.tax_id),
        # 2000B / 2010BA subscriber, 2010BB payer
        seg("HL", 2, 1, 22, 0),
        seg("SBR", "P", "18", "", "", "", "", "", "", "CI"),
        seg("NM1", "IL", 1, sub.last_name, sub.first_name, "", "", "", "MI", sub.member_id),
        seg("N3", sub.address.line1),
        seg("N4", sub.address.city, sub.address.state, sub.address.zip),
        seg("DMG", "D8", format_d8(sub.dob), sub.gender),
        seg("NM1", "PR", 2, payer.name, "", "", "", "", "PI", payer.payer_id),
    ]


def _hi(claim) -> list[str]:
    first, *rest = claim.diagnosis_codes
    return [seg("HI", comp("ABK", first), *(comp("ABF", c) for c in rest))]


def _professional_body(c: ProfessionalClaim) -> list[str]:
    out = [seg("CLM", c.claim_id, _amt(c.total_charge), "", "", comp(c.place_of_service, "B", c.frequency_code),
               "Y", "A", "Y", "Y")]
    out += _hi(c)
    for ln in c.lines:
        out += [
            seg("LX", ln.line_number),
            seg("SV1", comp("HC", ln.procedure_code, *ln.modifiers), _amt(ln.charge), "UN", _amt(ln.units),
                ln.place_of_service or "", "", comp(*ln.diagnosis_pointers)),
            seg("DTP", "472", "D8", format_d8(ln.service_date)),
        ]
    return out


def _institutional_body(c: InstitutionalClaim) -> list[str]:
    out = [seg("CLM", c.claim_id, _amt(c.total_charge), "", "", comp(c.bill_type[1:3], "A", c.bill_type[3]),
               "", "A", "Y", "Y"),
           seg("DTP", "434", "RD8", format_rd8(c.statement_from, c.statement_to))]
    if c.admission_date:
        out.append(seg("DTP", "435", "D8", format_d8(c.admission_date)))
    out.append(seg("CL1", c.admission_type or "", c.admission_source or "", c.patient_status))
    out += _hi(c)
    for ln in c.lines:
        proc = comp("HC", ln.procedure_code, *ln.modifiers) if ln.procedure_code else ""
        out += [
            seg("LX", ln.line_number),
            seg("SV2", ln.revenue_code, proc, _amt(ln.charge), "UN", _amt(ln.units)),
            seg("DTP", "472", "D8", format_d8(ln.service_date)),
        ]
    return out


def write_837(claim, control_number: int, ts: datetime | None = None) -> str:
    ts = ts or datetime.now()
    body = _header(claim, ts)
    body += _professional_body(claim) if isinstance(claim, ProfessionalClaim) else _institutional_body(claim)
    return wrap_interchange(body, "837", envelope(claim.claim_type), control_number, ts)
