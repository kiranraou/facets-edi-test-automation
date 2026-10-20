"""Turn Facets extract rows into candidates and add negative probes (not-found, invalid ID)."""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Iterable

import pandas as pd

from facets_qa.common.dates import to_date


def group_extract_rows(rows: Iterable[dict], service_date: date) -> pd.DataFrame:
    """Collapse the one-row-per-span extract into one row per member with a `spans` list column."""
    members: dict[int, dict] = {}
    for r in rows:
        ck = int(r["MEME_CK"])
        m = members.get(ck)
        if m is None:
            m = members[ck] = {
                "meme_ck": ck,
                "member_id": r["SBSB_ID"],
                "first_name": r["MEME_FIRST_NAME"],
                "last_name": r["MEME_LAST_NAME"],
                "dob": to_date(r["MEME_BIRTH_DT"]),
                "gender": r["MEME_SEX"],
                "service_date": service_date,
                "in_facets": True,
                "probe": None,
                "spans": [],
            }
        if r.get("MEPE_EFF_DT") is not None:
            m["spans"].append({
                "eff_dt": to_date(r["MEPE_EFF_DT"]),
                "term_dt": to_date(r["MEPE_TERM_DT"]),
                "elig_ind": r["MEPE_ELIG_IND"],
                "cspd_cat": r["CSPD_CAT"],
                "pdpd_id": r["PDPD_ID"],
            })
    return pd.DataFrame(list(members.values()))


def negative_probes(n_not_found: int, n_invalid_id: int, service_date: date, seed: int = 72) -> pd.DataFrame:
    """Synthetic members that must come back as AAA rejections."""
    rng = random.Random(seed)
    rows = []
    for i in range(n_not_found):
        rows.append(_probe(f"{990_000_000 + i:09d}", "member_not_found", service_date, rng))
    for i in range(n_invalid_id):
        # Facets subscriber IDs are 9 numeric digits; these are well-formed X12 but invalid for the payer.
        bad_id = rng.choice(["INV", "XX", "ABC"]) + f"{rng.randint(0, 99999):05d}"
        rows.append(_probe(bad_id, "invalid_member_id", service_date, rng))
    return pd.DataFrame(rows)


def _probe(member_id: str, probe: str, service_date: date, rng: random.Random) -> dict:
    return {
        "meme_ck": None,
        "member_id": member_id,
        "first_name": "PROBE",
        "last_name": "NEGATIVE",
        "dob": service_date - timedelta(days=rng.randint(365 * 20, 365 * 60)),
        "gender": "U",
        "service_date": service_date,
        "in_facets": False,
        "probe": probe,
        "spans": [],
    }
