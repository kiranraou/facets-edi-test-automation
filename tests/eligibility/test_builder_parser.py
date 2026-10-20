from __future__ import annotations

from datetime import date, datetime

import pytest

from facets_qa.common.x12 import X12ParseError, parse_segments, validate_envelope
from facets_qa.eligibility.builder_270 import build_270, default_envelope
from facets_qa.eligibility.models import Eligibility270Request
from facets_qa.eligibility.parser_271 import coverage_window, parse_271

TS = datetime(2026, 3, 1, 9, 30)


# --------------------------------------------------------------------- 270 builder
@pytest.mark.builder
def test_270_structure(request_270_kwargs):
    req = Eligibility270Request(**request_270_kwargs)
    segments = parse_segments(build_270(req, default_envelope(), 7, TS))
    validate_envelope(segments, "270")
    by_tag = {}
    for s in segments:
        by_tag.setdefault(s[0], []).append(s)

    assert by_tag["ST"][0] == ["ST", "270", "0007", "005010X279A1"]
    assert by_tag["GS"][0][1] == "HS"
    assert [s[3] for s in by_tag["HL"]] == ["20", "21", "22"]
    assert by_tag["TRN"][0][:3] == ["TRN", "1", req.trace_number]
    nm1 = {s[1]: s for s in by_tag["NM1"]}
    assert nm1["1P"][8:10] == ["XX", req.provider_npi]
    assert nm1["IL"][3:5] == ["TESTER", "ALEX"] and nm1["IL"][9] == "800000001"
    assert by_tag["DMG"][0] == ["DMG", "D8", "19800517"]
    assert by_tag["DTP"][0] == ["DTP", "291", "D8", req.service_date.strftime("%Y%m%d")]
    assert by_tag["EQ"][0] == ["EQ", "30"]


@pytest.mark.builder
def test_270_multiple_service_types_use_repetition_separator(request_270_kwargs):
    request_270_kwargs["service_type_codes"] = ("30", "1", "98")
    raw = build_270(Eligibility270Request(**request_270_kwargs), default_envelope(), 1, TS)
    assert "EQ*30^1^98~" in raw


# --------------------------------------------------------------------- 271 parser
ACTIVE_271 = """ISA*00*          *00*          *ZZ*ELIGGATEWAY    *ZZ*FACETSQA       *260301*0930*^*00501*000000007*0*T*:~
GS*HB*ELIGGATEWAY*FACETSQA*20260301*0930*7*X*005010X279A1~
ST*271*0007*005010X279A1~
BHT*0022*11*RUN1-800000001*20260301*0930~
HL*1**20*1~
NM1*PR*2*FACETS HEALTH PLAN*****PI*FACETS01~
HL*2*1*21*1~
NM1*1P*2*FACETS QA CLINIC*****XX*1234567893~
HL*3*2*22*0~
TRN*2*RUN1-800000001*9FACETSQA1~
NM1*IL*1*TESTER*ALEX****MI*800000001~
DMG*D8*19800517~
DTP*346*D8*20240101~
EB*1*IND*30^1*HM*PPO GOLD~
DTP*291*RD8*20250101-20261231~
EB*C*IND*30****500~
SE*15*0007~
GE*1*7~
IEA*1*000000007~
"""

REJECT_271 = """ST*271*0001*005010X279A1~
BHT*0022*11*T2*20260301*0930~
HL*1**20*1~
NM1*PR*2*FACETS HEALTH PLAN*****PI*FACETS01~
HL*2*1*21*1~
NM1*1P*2*CLINIC*****XX*1234567893~
HL*3*2*22*0~
TRN*2*T2*9FACETSQA1~
NM1*IL*1*NEGATIVE*PROBE****MI*INV00001~
AAA*N**72*C~
SE*11*0001~
"""


@pytest.mark.parser
def test_parse_active_271_with_rd8_dates_and_benefit_loops():
    r = parse_271(ACTIVE_271)
    assert r.trace_number == "RUN1-800000001"
    assert (r.payer_id, r.member_id, r.member_first, r.member_last) == ("FACETS01", "800000001", "ALEX", "TESTER")
    assert r.member_dob == date(1980, 5, 17)
    assert r.plan_begin == date(2024, 1, 1)
    assert r.coverage_status == "ACTIVE"
    assert len(r.benefits) == 2
    eb = r.benefits[0]
    assert eb.service_type_codes == ("30", "1")
    assert eb.plan_description == "PPO GOLD"
    assert coverage_window(r) == (date(2025, 1, 1), date(2026, 12, 31))
    assert r.benefits[1].coverage_start == date(2024, 1, 1)  # inherits subscriber-level DTP*346


@pytest.mark.parser
def test_parse_aaa_rejection():
    r = parse_271(REJECT_271)
    assert r.coverage_status == "REJECTED"
    rej = r.rejections[0]
    assert (rej.loop, rej.valid_request, rej.reject_reason, rej.follow_up_action) == ("2100C", "N", "72", "C")


@pytest.mark.parser
def test_parse_inactive_271_eb6():
    raw = REJECT_271.replace("AAA*N**72*C~", "EB*6*IND*30~\nDTP*291*RD8*20230101-20231231~").replace("SE*11", "SE*12")
    r = parse_271(raw)
    assert r.coverage_status == "INACTIVE"
    assert (r.benefits[0].coverage_start, r.benefits[0].coverage_end) == (date(2023, 1, 1), date(2023, 12, 31))


@pytest.mark.parser
@pytest.mark.parametrize("raw,message", [
    ("ST*270*0001~SE*2*0001~", "No ST\\*271"),
    (REJECT_271.replace("SE*11", "SE*10"), "segment count"),
    (REJECT_271.replace("AAA*N**72*C~\n", "").replace("SE*11", "SE*10"), "neither EB benefit loops nor AAA"),
    (REJECT_271.replace("AAA*N**72*C~", "AAA*N**7*C~"), "contract validation"),
    (REJECT_271.replace("AAA*N**72*C~", "EB*1~\nDTP*291*RD8*20251231-20250101~").replace("SE*11", "SE*12"),
     "ends before it starts"),
])
def test_parser_rejects_malformed_271(raw, message):
    with pytest.raises((X12ParseError, ValueError), match=message):
        parse_271(raw)
