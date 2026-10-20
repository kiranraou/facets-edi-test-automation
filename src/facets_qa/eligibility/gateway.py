"""Eligibility gateways: the system under test that answers 270s with 271s.

* ``HttpEligibilityGateway`` posts the 270 to the real clearinghouse / Facets
  real-time eligibility endpoint (used in Databricks).
* ``MockEligibilityGateway`` answers from a Facets snapshot so the framework
  runs locally; it can inject transient failures (to exercise retries) and
  data drift (to demonstrate how defects surface as FAIL rows).
"""

from __future__ import annotations

import random
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from facets_qa.common.dates import FACETS_HIGH_DATE, format_d8, format_rd8, parse_d8
from facets_qa.common.retry import GatewayTimeout, GatewayUnavailable
from facets_qa.common.x12 import EnvelopeConfig, element, parse_segments, seg, wrap_interchange

FACETS_MEMBER_ID = re.compile(r"^\d{9}$")
PLAN_NAMES = {"MED00001": "PPO GOLD", "MED00002": "HMO SILVER", "MED00003": "EPO BRONZE"}


class EligibilityGateway(Protocol):
    def send(self, x12_270: str) -> str: ...


# --------------------------------------------------------------------- HTTP (real)
class HttpEligibilityGateway:
    TRANSIENT_STATUS = {408, 429, 502, 503, 504}

    def __init__(self, url: str, token: str | None = None, timeout_s: float = 15.0):
        self.url, self.token, self.timeout_s = url, token, timeout_s

    def send(self, x12_270: str) -> str:
        headers = {"Content-Type": "application/edi-x12"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=x12_270.encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code in self.TRANSIENT_STATUS:
                raise GatewayUnavailable(f"HTTP {exc.code}") from exc
            raise
        except (TimeoutError, urllib.error.URLError) as exc:
            raise GatewayTimeout(str(exc)) from exc


# --------------------------------------------------------------------- Mock (local)
@dataclass(frozen=True)
class SnapshotMember:
    member_id: str
    first_name: str
    last_name: str
    dob: date
    spans: tuple[tuple[date, date, str, str], ...]  # (eff, term, elig_ind, pdpd_id)


@dataclass
class MockEligibilityGateway:
    members: dict[str, SnapshotMember]
    payer_id: str = "FACETS01"
    payer_name: str = "FACETS HEALTH PLAN"
    transient_failure_rate: float = 0.0
    drift_member_ids: frozenset[str] = frozenset()
    seed: int = 271
    calls: int = 0
    _rng: random.Random = field(init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        self._rng = random.Random(self.seed)

    def send(self, x12_270: str) -> str:
        with self._lock:
            self.calls += 1
            roll = self._rng.random()
        if roll < self.transient_failure_rate:
            raise (GatewayTimeout if roll < self.transient_failure_rate / 2 else GatewayUnavailable)(
                "simulated clearinghouse failure")
        return self._respond(x12_270)

    def _respond(self, x12_270: str) -> str:
        req = _read_270(x12_270)
        member = self.members.get(req["member_id"])
        if not FACETS_MEMBER_ID.match(req["member_id"]):
            body = self._subscriber_header(req) + [seg("AAA", "N", "", "72", "C")]
        elif member is None:
            body = self._subscriber_header(req) + [seg("AAA", "N", "", "75", "C")]
        elif member.dob != req["dob"]:
            body = self._subscriber_header(req) + [seg("AAA", "N", "", "71", "C")]
        else:
            body = self._subscriber_header(req, member) + self._benefits(member, req["service_date"])
        env = EnvelopeConfig(sender_id="ELIGGATEWAY", receiver_id="FACETSQA",
                             functional_id="HB", implementation_ref="005010X279A1")
        return wrap_interchange(self._source_loops(req) + body, "271", env, req["control"], datetime.now())

    def _source_loops(self, req: dict) -> list[str]:
        return [
            seg("BHT", "0022", "11", req["trace"], datetime.now().strftime("%Y%m%d"),
                datetime.now().strftime("%H%M")),
            seg("HL", 1, "", 20, 1),
            seg("NM1", "PR", 2, self.payer_name, "", "", "", "", "PI", self.payer_id),
            seg("HL", 2, 1, 21, 1),
            seg("NM1", "1P", 2, req["provider_name"], "", "", "", "", "XX", req["npi"]),
        ]

    def _subscriber_header(self, req: dict, member: SnapshotMember | None = None) -> list[str]:
        last = member.last_name if member else req["last"]
        first = member.first_name if member else req["first"]
        out = [
            seg("HL", 3, 2, 22, 0),
            seg("TRN", 2, req["trace"], req["trace_originator"]),
            seg("NM1", "IL", 1, last, first, "", "", "", "MI", req["member_id"]),
        ]
        if member:
            out.append(seg("DMG", "D8", format_d8(member.dob)))
        return out

    def _benefits(self, member: SnapshotMember, svc: date) -> list[str]:
        covering = [s for s in member.spans if s[0] <= svc <= s[1] and s[2] == "Y"]
        active = bool(covering)
        if member.member_id in self.drift_member_ids:
            active = not active  # simulated defect: gateway out of sync with Facets
        if active and covering:
            eff, term, _, pdpd = covering[0]
            dtp = (seg("DTP", "346", "D8", format_d8(eff)) if term == FACETS_HIGH_DATE
                   else seg("DTP", "291", "RD8", format_rd8(eff, term)))
            return [seg("EB", "1", "IND", "30", "HM", PLAN_NAMES.get(pdpd, pdpd)), dtp]
        if active:  # drifted to active with no real span
            return [seg("EB", "1", "IND", "30", "HM", "UNKNOWN PLAN")]
        last = max(member.spans, key=lambda s: s[0], default=None)
        out = [seg("EB", "6", "IND", "30")]
        if last:
            out.append(seg("DTP", "291", "RD8", format_rd8(last[0], last[1])))
        return out


def _read_270(x12_270: str) -> dict:
    out = {"control": 1, "trace": "", "trace_originator": "", "member_id": "", "first": "", "last": "",
           "dob": None, "service_date": date.today(), "npi": "", "provider_name": ""}
    for s in parse_segments(x12_270):
        tag = s[0]
        if tag == "ISA":
            out["control"] = int(element(s, 13) or 1)
        elif tag == "TRN":
            out["trace"], out["trace_originator"] = element(s, 2), element(s, 3)
        elif tag == "NM1" and element(s, 1) == "IL":
            out["last"], out["first"], out["member_id"] = element(s, 3), element(s, 4), element(s, 9)
        elif tag == "NM1" and element(s, 1) == "1P":
            out["provider_name"], out["npi"] = element(s, 3), element(s, 9)
        elif tag == "DMG":
            out["dob"] = parse_d8(element(s, 2))
        elif tag == "DTP" and element(s, 1) == "291":
            out["service_date"] = parse_d8(element(s, 3))
    return out


def snapshot_from_candidates(rows) -> dict[str, SnapshotMember]:
    """Build the mock gateway's member snapshot from grouped Facets candidate rows."""
    snap = {}
    for r in rows:
        if not r["in_facets"]:
            continue
        spans = tuple((sp["eff_dt"], sp["term_dt"], sp["elig_ind"], sp.get("pdpd_id") or "")
                      for sp in r["spans"] if sp.get("cspd_cat", "M") == "M")
        snap[r["member_id"]] = SnapshotMember(r["member_id"], r["first_name"], r["last_name"], r["dob"], spans)
    return snap
