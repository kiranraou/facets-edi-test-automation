"""Replicate synthetic test data into Facets and simulate 837 intake into claim tables.

Locally this stands in for the EDI gateway -> Facets claims intake. Against a
real Facets environment, the 837 files go to the inbound EDI folder instead,
and only ``validate_claim`` (facets_validation.py) runs against SQL Server.
"""

from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from facets_qa.claims.models import BillingProvider, Subscriber
from facets_qa.common.dates import parse_d8, parse_rd8
from facets_qa.common.x12 import detect_delimiters, element, parse_segments, validate_envelope


# --------------------------------------------------------------------- replication
def replicate_subscriber(conn: sqlite3.Connection, sub: Subscriber) -> int:
    """Insert (or reuse) a synthetic subscriber in CMC_SBSB_SUBSC / CMC_MEME_MEMBER; returns MEME_CK."""
    row = conn.execute("SELECT m.MEME_CK FROM CMC_MEME_MEMBER m JOIN CMC_SBSB_SUBSC s ON s.SBSB_CK = m.SBSB_CK "
                       "WHERE s.SBSB_ID = ? AND m.MEME_SFX = 0", (sub.member_id,)).fetchone()
    if row:
        return row[0]
    ck = int(sub.member_id)  # test IDs are 9-digit numeric; reuse as the surrogate key
    conn.execute("INSERT INTO CMC_SBSB_SUBSC VALUES (?,?,?,?,?,?)",
                 (ck, 9001, sub.member_id, sub.last_name, sub.first_name, "2020-01-01"))
    conn.execute("INSERT INTO CMC_MEME_MEMBER VALUES (?,?,?,?,?,?,?,?)",
                 (ck, ck, 0, "M", sub.last_name, sub.first_name, sub.dob.isoformat(), sub.gender))
    conn.execute("INSERT INTO CMC_MEPE_PRCS_ELIG VALUES (?,?,?,?,?,?,?)",
                 (ck, "M", "2020-01-01", "9999-12-31", "Y", "CSPI0001", "MED00001"))
    return ck


def replicate_provider(conn: sqlite3.Connection, prov: BillingProvider) -> str:
    row = conn.execute("SELECT PRPR_ID FROM CMC_PRPR_PROV WHERE PRPR_NPI = ?", (prov.npi,)).fetchone()
    if row:
        return row[0]
    prpr_id = f"P{prov.npi}"
    conn.execute("INSERT INTO CMC_PRPR_PROV VALUES (?,?,?,?)", (prpr_id, prov.npi, prov.name, prov.tax_id))
    return prpr_id


# --------------------------------------------------------------------- intake
@dataclass
class _Line:
    seq: int
    ipcd: Optional[str] = None
    mods: tuple[str, ...] = ()
    rcrc: Optional[str] = None
    charge: Decimal = Decimal("0")
    units: Decimal = Decimal("1")
    dos: Optional[date] = None


class FacetsClaimsIntake:
    """Parses 837 interchanges and writes CMC_CLCL_CLAIM / CMC_CDML_CL_LINE / CMC_CLHP_HOSP rows.

    ``defects`` maps a patient control number (CLM01) to a simulated intake
    defect so the validation layer can be shown catching it.
    """

    DEFECTS = {"drop_last_modifier", "charge_off_by_cent", "wrong_admission_date"}

    def __init__(self, conn: sqlite3.Connection, input_date: date | None = None,
                 defects: dict[str, str] | None = None):
        self.conn = conn
        self.input_date = input_date or date.today()
        self.defects = defects or {}
        unknown = set(self.defects.values()) - self.DEFECTS
        if unknown:
            raise ValueError(f"Unknown defects: {unknown}")
        self._seq = itertools.count(1)

    def _next_clcl_id(self) -> str:
        return f"{self.input_date:%y%j}{next(self._seq):05d}00"  # 12 chars, Facets style

    def load_837(self, raw: str) -> list[str]:
        segments = parse_segments(raw)
        validate_envelope(segments, "837")
        d = detect_delimiters(raw)
        impl = next(element(s, 3) for s in segments if s[0] == "ST")
        sub_type = "M" if impl.endswith("X222A1") else "H"

        clcl_ids, claim, lines, npi, member_id = [], None, [], None, None
        for s in segments:
            tag = s[0]
            if tag == "NM1" and element(s, 1) == "85":
                npi = element(s, 9)
            elif tag == "NM1" and element(s, 1) == "IL":
                member_id = element(s, 9)
            elif tag == "CLM":
                if claim:
                    clcl_ids.append(self._write(claim, lines))
                facility = element(s, 5).split(d.component)
                claim = {"acct": element(s, 1), "total": Decimal(element(s, 2)), "sub_type": sub_type,
                         "npi": npi, "member_id": member_id, "pos": facility[0] if sub_type == "M" else None,
                         "bill_type": f"0{facility[0]}{facility[2]}" if sub_type == "H" else None}
                lines = []
            elif tag == "DTP" and claim is not None:
                qual, fmt, val = element(s, 1), element(s, 2), element(s, 3)
                if qual == "472" and lines:
                    lines[-1].dos = parse_d8(val)
                elif qual == "434":
                    claim["stmt"] = parse_rd8(val)
                elif qual == "435":
                    claim["admit"] = parse_d8(val)
            elif tag == "CL1" and claim is not None:
                claim["adm_type"], claim["status"] = element(s, 1) or None, element(s, 3) or None
            elif tag == "LX":
                lines.append(_Line(seq=int(element(s, 1))))
            elif tag in ("SV1", "SV2") and lines:
                ln = lines[-1]
                proc = element(s, 1 if tag == "SV1" else 2)
                charge_idx, units_idx = (2, 4) if tag == "SV1" else (3, 5)
                if proc:
                    parts = proc.split(d.component)
                    ln.ipcd, ln.mods = parts[1], tuple(parts[2:])
                if tag == "SV2":
                    ln.rcrc = element(s, 1)
                ln.charge, ln.units = Decimal(element(s, charge_idx)), Decimal(element(s, units_idx) or "1")
        if claim:
            clcl_ids.append(self._write(claim, lines))
        self.conn.commit()
        return clcl_ids

    def _write(self, claim: dict, lines: list[_Line]) -> str:
        defect = self.defects.get(claim["acct"])
        meme = self.conn.execute(
            "SELECT m.MEME_CK FROM CMC_MEME_MEMBER m JOIN CMC_SBSB_SUBSC s ON s.SBSB_CK = m.SBSB_CK "
            "WHERE s.SBSB_ID = ?", (claim["member_id"],)).fetchone()
        prov = self.conn.execute("SELECT PRPR_ID FROM CMC_PRPR_PROV WHERE PRPR_NPI = ?", (claim["npi"],)).fetchone()
        if meme is None or prov is None:
            raise LookupError(f"Claim {claim['acct']}: member or provider not replicated in Facets")

        if defect == "charge_off_by_cent":
            lines[0].charge += Decimal("0.01")
        if defect == "drop_last_modifier" and lines[0].mods:
            lines[0].mods = lines[0].mods[:-1]

        clcl_id = self._next_clcl_id()
        low, high = claim.get("stmt") or (min(ln.dos for ln in lines), max(ln.dos for ln in lines))
        self.conn.execute(
            "INSERT INTO CMC_CLCL_CLAIM VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (clcl_id, claim["acct"], "M", claim["sub_type"], "01", meme[0], prov[0], str(claim["total"]),
             low.isoformat(), high.isoformat(), claim["pos"], self.input_date.isoformat()))
        for ln in lines:
            mods = list(ln.mods) + [None] * (4 - len(ln.mods))
            self.conn.execute(
                "INSERT INTO CMC_CDML_CL_LINE VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (clcl_id, ln.seq, ln.ipcd, *mods, ln.rcrc, str(ln.charge), str(ln.units),
                 ln.dos.isoformat(), ln.dos.isoformat()))
        if claim["sub_type"] == "H":
            bt = claim["bill_type"]
            admit = claim.get("admit")
            if defect == "wrong_admission_date" and admit:
                admit = admit.replace(day=1)
            discharged = claim.get("status") not in (None, "30")  # 30 = still a patient
            self.conn.execute(
                "INSERT INTO CMC_CLHP_HOSP VALUES (?,?,?,?,?,?,?,?)",
                (clcl_id, bt[1], bt[2], bt[3], admit.isoformat() if admit else None,
                 high.isoformat() if discharged and admit else None, claim.get("adm_type"), claim.get("status")))
        return clcl_id
