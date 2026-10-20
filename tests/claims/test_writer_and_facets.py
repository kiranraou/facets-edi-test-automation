from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from facets_qa.claims.facets_loader import FacetsClaimsIntake, replicate_provider, replicate_subscriber
from facets_qa.claims.facets_validation import validate_claim
from facets_qa.claims.models import ClaimAdapter
from facets_qa.claims.pipeline import run_all
from facets_qa.claims.scenarios import SCENARIOS, by_name
from facets_qa.claims.writer_837 import write_837
from facets_qa.common.x12 import parse_segments, validate_envelope

DOS = date.today() - timedelta(days=14)
TS = datetime(2026, 3, 1, 10, 0)


def build(factory, name):
    return ClaimAdapter.validate_python(by_name(name).build(factory, DOS))


def segs(raw, tag):
    return [s for s in parse_segments(raw) if s[0] == tag]


# --------------------------------------------------------------------- 837 output
@pytest.mark.x12
@pytest.mark.parametrize("sc", [s for s in SCENARIOS if s.polarity == "positive"], ids=lambda s: s.name)
def test_837_envelopes_are_valid(factory, sc):
    claim = ClaimAdapter.validate_python(sc.build(factory, DOS))
    raw = write_837(claim, 1, TS)
    validate_envelope(parse_segments(raw), "837")
    st = segs(raw, "ST")[0]
    assert st[3] == {"837P": "005010X222A1", "837I": "005010X223A2"}[sc.claim_type]
    assert segs(raw, "CLM")[0][1] == claim.claim_id


@pytest.mark.x12
def test_837p_modifier_25_segments(factory):
    claim = build(factory, "office_visit_modifier_25")
    raw = write_837(claim, 1, TS)
    clm = segs(raw, "CLM")[0]
    assert clm[2] == "225" and clm[5] == "11:B:1"
    assert segs(raw, "HI")[0] == ["HI", "ABK:E119", "ABF:R002"]
    sv1 = segs(raw, "SV1")
    assert sv1[0][1] == "HC:99214:25" and sv1[0][7] == "1:2"
    assert sv1[1][1] == "HC:93000" and sv1[1][2] == "45"
    assert [s[1] for s in segs(raw, "LX")] == ["1", "2"]
    assert segs(raw, "NM1")[2][8:10] == ["XX", claim.billing_provider.npi]


@pytest.mark.x12
def test_837p_telehealth_and_units(factory):
    tele = write_837(build(factory, "telehealth_pos_10"), 1, TS)
    assert segs(tele, "CLM")[0][5] == "10:B:1"
    assert segs(tele, "SV1")[0][1] == "HC:99213:95"
    units = write_837(build(factory, "multi_unit_therapy"), 1, TS)
    sv1 = segs(units, "SV1")[0]
    assert (sv1[1], sv1[2], sv1[3], sv1[4]) == ("HC:97110:GP", "240", "UN", "4")


@pytest.mark.x12
def test_837i_inpatient_segments(factory):
    claim = build(factory, "inpatient_0111")
    raw = write_837(claim, 1, TS)
    assert segs(raw, "CLM")[0][5] == "11:A:1"
    dtp = {s[1]: s for s in segs(raw, "DTP")}
    assert dtp["434"][2] == "RD8" and dtp["435"][3] == claim.admission_date.strftime("%Y%m%d")
    assert segs(raw, "CL1")[0] == ["CL1", "1", "7", "01"]
    sv2 = segs(raw, "SV2")
    assert [s[1] for s in sv2] == ["0120", "0250", "0300"]
    assert sv2[2][2] == "HC:80053" and sv2[0][3] == "3600"


@pytest.mark.x12
def test_837i_outpatient_has_no_admission_date(factory):
    raw = write_837(build(factory, "outpatient_0131"), 1, TS)
    assert segs(raw, "CLM")[0][5] == "13:A:1"
    assert "435" not in {s[1] for s in segs(raw, "DTP")}


# --------------------------------------------------------------------- replication + Facets validation
def load(conn, claim, defects=None):
    meme_ck = replicate_subscriber(conn, claim.subscriber)
    prpr_id = replicate_provider(conn, claim.billing_provider)
    clcl_id = FacetsClaimsIntake(conn, defects=defects).load_837(write_837(claim, 1, TS))[0]
    return clcl_id, meme_ck, prpr_id


@pytest.mark.facets
def test_replication_is_idempotent(facets_conn, factory):
    claim = build(factory, "office_visit_established")
    assert replicate_subscriber(facets_conn, claim.subscriber) == replicate_subscriber(facets_conn, claim.subscriber)
    assert replicate_provider(facets_conn, claim.billing_provider) == replicate_provider(facets_conn,
                                                                                         claim.billing_provider)
    assert facets_conn.execute("SELECT COUNT(*) FROM CMC_MEME_MEMBER").fetchone()[0] == 1


@pytest.mark.facets
@pytest.mark.parametrize("name", ["office_visit_modifier_25", "multi_unit_therapy", "inpatient_0111",
                                  "outpatient_0131"])
def test_facets_rows_match_claim(facets_conn, factory, name):
    claim = build(factory, name)
    clcl_id, meme_ck, prpr_id = load(facets_conn, claim)
    assert validate_claim(facets_conn, claim, clcl_id, meme_ck, prpr_id) == []
    hdr = facets_conn.execute("SELECT CLCL_CL_SUB_TYPE, CLCL_TOT_CHG FROM CMC_CLCL_CLAIM").fetchone()
    assert hdr[0] == ("M" if claim.claim_type == "837P" else "H")


@pytest.mark.facets
@pytest.mark.parametrize("scenario,defect,table,column", [
    ("office_visit_modifier_25", "drop_last_modifier", "CMC_CDML_CL_LINE", "CDML_MOD1"),
    ("office_visit_established", "charge_off_by_cent", "CMC_CDML_CL_LINE", "CDML_CHG_AMT"),
    ("inpatient_0111", "wrong_admission_date", "CMC_CLHP_HOSP", "CLHP_ADM_DT"),
])
def test_field_level_mismatches_are_reported(facets_conn, factory, scenario, defect, table, column):
    claim = build(factory, scenario)
    clcl_id, meme_ck, prpr_id = load(facets_conn, claim, defects={claim.claim_id: defect})
    mismatches = validate_claim(facets_conn, claim, clcl_id, meme_ck, prpr_id)
    assert [(m.table, m.column) for m in mismatches] == [(table, column)]
    assert "expected" in str(mismatches[0])


@pytest.mark.facets
def test_missing_claim_is_reported(facets_conn, factory):
    claim = build(factory, "office_visit_established")
    mismatches = validate_claim(facets_conn, claim, "NOPE00000000", 1, "P1")
    assert mismatches[0].column == "*"


@pytest.mark.facets
def test_unreplicated_member_cannot_be_loaded(facets_conn, factory):
    claim = build(factory, "office_visit_established")
    with pytest.raises(LookupError):
        FacetsClaimsIntake(facets_conn).load_837(write_837(claim, 1, TS))


@pytest.mark.integration
def test_full_claims_suite(facets_conn):
    results = run_all(facets_conn, DOS)
    assert {r.result for r in results} == {"PASS"}, [r for r in results if r.result != "PASS"]
    loaded = facets_conn.execute("SELECT COUNT(*) FROM CMC_CLCL_CLAIM").fetchone()[0]
    assert loaded == sum(r.polarity == "positive" for r in results)  # negatives never reach Facets
