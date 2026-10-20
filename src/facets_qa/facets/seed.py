"""Deterministic, PHI-safe membership seed data for the mock Facets database.

Every member gets eligibility spans shaped to land in one of the eligibility
test scenarios relative to the run's service date.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

from facets_qa.common.dates import FACETS_HIGH_DATE

FIRST_NAMES = ["ALEX", "JORDAN", "TAYLOR", "MORGAN", "CASEY", "RILEY", "JAMIE", "AVERY",
               "QUINN", "SKYLER", "DREW", "REESE", "PARKER", "ROWAN", "SAGE", "EMERSON"]
LAST_NAMES = ["TESTER", "SAMPLE", "MOCKSON", "DUMMY", "FIXTURE", "SYNTH", "PLACEHOLDER",
              "QADATA", "STUBBS", "PROBE", "VALIDA", "CHECKLEY"]
PLANS = [("MED00001", "PPO GOLD"), ("MED00002", "HMO SILVER"), ("MED00003", "EPO BRONZE")]

# Share of seeded members per span shape.
SCENARIO_MIX = [
    ("active", 0.66),
    ("terminated", 0.12),
    ("future_effective", 0.08),
    ("elig_ind_off", 0.06),
    ("reinstated", 0.05),     # termed span followed by a current span -> active
    ("coverage_gap", 0.03),   # service date falls between two spans -> inactive
]


def _pick_shape(rng: random.Random) -> str:
    r, acc = rng.random(), 0.0
    for shape, weight in SCENARIO_MIX:
        acc += weight
        if r < acc:
            return shape
    return SCENARIO_MIX[0][0]


def _spans(shape: str, svc: date, rng: random.Random) -> list[tuple[date, date, str]]:
    def days(a: int, b: int) -> timedelta:
        return timedelta(days=rng.randint(a, b))

    if shape == "active":
        eff = svc - days(30, 1500)
        term = FACETS_HIGH_DATE if rng.random() < 0.7 else svc + days(1, 365)
        return [(eff, term, "Y")]
    if shape == "terminated":
        term = svc - days(1, 400)
        return [(term - days(90, 900), term, "Y")]
    if shape == "future_effective":
        return [(svc + days(1, 120), FACETS_HIGH_DATE, "Y")]
    if shape == "elig_ind_off":
        return [(svc - days(30, 700), FACETS_HIGH_DATE, "N")]
    if shape == "reinstated":
        old_term = svc - days(60, 300)
        return [(old_term - days(200, 600), old_term, "Y"),
                (old_term + days(15, 50), FACETS_HIGH_DATE, "Y")]
    if shape == "coverage_gap":
        gap_start = svc - days(5, 40)
        return [(gap_start - days(200, 600), gap_start, "Y"),
                (svc + days(5, 60), FACETS_HIGH_DATE, "Y")]
    raise ValueError(shape)


def generate_member_rows(count: int, service_date: date, seed: int = 270):
    """Return (subscribers, members, eligibility) row tuples with date objects, in DDL column order."""
    rng = random.Random(seed)
    subsc, members, elig = [], [], []
    for i in range(count):
        sbsb_ck = meme_ck = 100_000 + i
        sbsb_id = f"{800_000_000 + i:09d}"
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        dob = date(1940, 1, 1) + timedelta(days=rng.randint(0, 365 * 80))
        if dob >= service_date:
            dob = service_date - timedelta(days=365)
        shape = _pick_shape(rng)
        spans = _spans(shape, service_date, rng)
        pdpd_id, _ = rng.choice(PLANS)
        subsc.append((sbsb_ck, 1001, sbsb_id, last, first, spans[0][0]))
        members.append((meme_ck, sbsb_ck, 0, "M", last, first, dob, rng.choice("MF")))
        for eff, term, ind in spans:
            elig.append((meme_ck, "M", eff, term, ind, "CSPI0001", pdpd_id))
    return subsc, members, elig


def _iso(rows):
    return [tuple(v.isoformat() if isinstance(v, date) else v for v in r) for r in rows]


def seed_members(conn: sqlite3.Connection, count: int, service_date: date, seed: int = 270) -> None:
    subsc, members, elig = generate_member_rows(count, service_date, seed)
    conn.executemany("INSERT INTO CMC_SBSB_SUBSC VALUES (?,?,?,?,?,?)", _iso(subsc))
    conn.executemany("INSERT INTO CMC_MEME_MEMBER VALUES (?,?,?,?,?,?,?,?)", _iso(members))
    conn.executemany("INSERT INTO CMC_MEPE_PRCS_ELIG VALUES (?,?,?,?,?,?,?)", _iso(elig))
    conn.commit()
