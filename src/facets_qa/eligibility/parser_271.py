"""Parse 271 eligibility responses into validated Eligibility271Response records."""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import ValidationError

from facets_qa.common.dates import parse_d8, parse_x12_date
from facets_qa.common.x12 import X12ParseError, detect_delimiters, element, parse_segments, validate_envelope
from facets_qa.eligibility.models import Eligibility271Response

HL_LEVEL_TO_LOOP = {"20": "2100A", "21": "2100B", "22": "2100C", "23": "2100D"}

# DTP qualifiers we understand: 291 plan, 307 eligibility, 346/356 begin, 347/357 end.
RANGE_QUALIFIERS = {"291", "307"}
BEGIN_QUALIFIERS = {"346", "356"}
END_QUALIFIERS = {"347", "357"}


def _apply_dtp(target: dict, qualifier: str, fmt: str, value: str, start_key: str, end_key: str) -> None:
    start, end = parse_x12_date(fmt, value)
    if qualifier in RANGE_QUALIFIERS:
        target[start_key] = start
        if fmt == "RD8":
            target[end_key] = end
    elif qualifier in BEGIN_QUALIFIERS:
        target[start_key] = start
    elif qualifier in END_QUALIFIERS:
        target[end_key] = end


def parse_271(raw: str) -> Eligibility271Response:
    segments = parse_segments(raw)
    validate_envelope(segments, "271")
    repetition = detect_delimiters(raw).repetition

    data: dict = {"benefits": [], "rejections": []}
    loop = "2100A"
    current_eb: Optional[dict] = None
    in_subscriber = False

    for s in segments:
        tag = s[0]
        if tag == "HL":
            loop = HL_LEVEL_TO_LOOP.get(element(s, 3), loop)
            in_subscriber = loop in ("2100C", "2100D")
            current_eb = None
        elif tag == "TRN" and in_subscriber and element(s, 1) == "2":
            data["trace_number"] = element(s, 2)
        elif tag == "NM1":
            entity = element(s, 1)
            if entity == "PR":
                data["payer_id"] = element(s, 9) or None
            elif entity == "IL":
                data["member_last"] = element(s, 3) or None
                data["member_first"] = element(s, 4) or None
                data["member_id"] = element(s, 9) or None
        elif tag == "DMG" and in_subscriber and element(s, 1) == "D8" and element(s, 2):
            data["member_dob"] = parse_d8(element(s, 2))
        elif tag == "AAA":
            data["rejections"].append({
                "loop": loop,
                "valid_request": element(s, 1),
                "reject_reason": element(s, 3),
                "follow_up_action": element(s, 4) or None,
            })
        elif tag == "EB":
            current_eb = {
                "eligibility_code": element(s, 1),
                "coverage_level": element(s, 2) or None,
                "service_type_codes": tuple(c for c in element(s, 3).split(repetition) if c),
                "insurance_type": element(s, 4) or None,
                "plan_description": element(s, 5) or None,
            }
            data["benefits"].append(current_eb)
        elif tag == "DTP" and in_subscriber:
            qualifier, fmt, value = element(s, 1), element(s, 2), element(s, 3)
            if current_eb is not None:
                _apply_dtp(current_eb, qualifier, fmt, value, "coverage_start", "coverage_end")
            else:
                _apply_dtp(data, qualifier, fmt, value, "plan_begin", "plan_end")
        elif tag == "SE":
            break  # real-time 271: one transaction per interchange

    # EB loops without their own dates inherit the subscriber-level plan dates.
    for eb in data["benefits"]:
        eb.setdefault("coverage_start", data.get("plan_begin"))
        eb.setdefault("coverage_end", data.get("plan_end"))

    try:
        return Eligibility271Response(**data)
    except ValidationError as exc:
        raise X12ParseError(f"271 failed contract validation: {exc}") from exc


def coverage_window(resp: Eligibility271Response) -> tuple[Optional[date], Optional[date]]:
    eb = resp.active_benefit()
    return (eb.coverage_start, eb.coverage_end) if eb else (None, None)
