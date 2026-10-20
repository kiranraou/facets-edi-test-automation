from __future__ import annotations

from datetime import date, timedelta

import pytest

from facets_qa.claims.generator import SyntheticFactory
from facets_qa.common.dates import FACETS_HIGH_DATE
from facets_qa.eligibility.gateway import SnapshotMember
from facets_qa.facets import db as facets_db

VALID_NPI = "1234567893"


@pytest.fixture
def service_date() -> date:
    return date.today() - timedelta(days=7)


@pytest.fixture
def facets_conn():
    conn = facets_db.connect(":memory:")
    facets_db.create_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def factory() -> SyntheticFactory:
    return SyntheticFactory(seed=837)


@pytest.fixture
def request_270_kwargs(service_date) -> dict:
    return dict(
        trace_number="RUN1-800000001", payer_id="FACETS01", payer_name="FACETS HEALTH PLAN",
        provider_npi=VALID_NPI, provider_name="FACETS QA CLINIC", member_id="800000001",
        member_first="ALEX", member_last="TESTER", member_dob=date(1980, 5, 17),
        service_date=service_date, service_type_codes=("30",),
    )


@pytest.fixture
def candidate_row(service_date) -> dict:
    return {
        "meme_ck": 100001, "member_id": "800000001", "first_name": "ALEX", "last_name": "TESTER",
        "dob": date(1980, 5, 17), "gender": "F", "service_date": service_date, "in_facets": True,
        "probe": None,
        "spans": [{"eff_dt": service_date - timedelta(days=200), "term_dt": FACETS_HIGH_DATE,
                   "elig_ind": "Y", "cspd_cat": "M", "pdpd_id": "MED00001"}],
    }


@pytest.fixture
def snapshot(candidate_row) -> dict[str, SnapshotMember]:
    r = candidate_row
    spans = tuple((s["eff_dt"], s["term_dt"], s["elig_ind"], s["pdpd_id"]) for s in r["spans"])
    return {r["member_id"]: SnapshotMember(r["member_id"], r["first_name"], r["last_name"], r["dob"], spans)}


@pytest.fixture
def no_sleep():
    calls: list[float] = []
    calls_append = calls.append

    def _sleep(seconds: float) -> None:
        calls_append(seconds)

    _sleep.calls = calls  # type: ignore[attr-defined]
    return _sleep
