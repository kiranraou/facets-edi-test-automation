"""Build HIPAA 5010 270 eligibility inquiries (005010X279A1)."""

from __future__ import annotations

from datetime import datetime

from facets_qa.common.dates import format_d8
from facets_qa.common.x12 import DEFAULT, EnvelopeConfig, seg, wrap_interchange
from facets_qa.eligibility.models import Eligibility270Request

IMPL_270 = "005010X279A1"
TRACE_ORIGINATOR = "9FACETSQA1"  # TRN03: '9' + originating company id


def default_envelope(sender_id: str = "FACETSQA", receiver_id: str = "ELIGGATEWAY") -> EnvelopeConfig:
    return EnvelopeConfig(sender_id=sender_id, receiver_id=receiver_id,
                          functional_id="HS", implementation_ref=IMPL_270)


def build_270(req: Eligibility270Request, env: EnvelopeConfig, control_number: int,
              ts: datetime | None = None) -> str:
    ts = ts or datetime.now()
    rep = DEFAULT.repetition
    body = [
        seg("BHT", "0022", "13", req.trace_number, ts.strftime("%Y%m%d"), ts.strftime("%H%M")),
        # 2000A / 2100A information source (payer)
        seg("HL", 1, "", 20, 1),
        seg("NM1", "PR", 2, req.payer_name, "", "", "", "", "PI", req.payer_id),
        # 2000B / 2100B information receiver (provider)
        seg("HL", 2, 1, 21, 1),
        seg("NM1", "1P", 2, req.provider_name, "", "", "", "", "XX", req.provider_npi),
        # 2000C / 2100C subscriber
        seg("HL", 3, 2, 22, 0),
        seg("TRN", 1, req.trace_number, TRACE_ORIGINATOR),
        seg("NM1", "IL", 1, req.member_last, req.member_first, "", "", "", "MI", req.member_id),
        seg("DMG", "D8", format_d8(req.member_dob)),
        seg("DTP", "291", "D8", format_d8(req.service_date)),
        # 2110C eligibility inquiry
        seg("EQ", rep.join(req.service_type_codes)),
    ]
    return wrap_interchange(body, "270", env, control_number, ts)
