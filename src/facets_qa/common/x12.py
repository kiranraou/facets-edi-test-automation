"""Minimal X12 5010 envelope writer and segment tokenizer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class X12ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Delimiters:
    segment: str = "~"
    element: str = "*"
    component: str = ":"
    repetition: str = "^"


DEFAULT = Delimiters()


@dataclass(frozen=True)
class EnvelopeConfig:
    sender_id: str
    receiver_id: str
    functional_id: str  # HS = 270, HB = 271, HC = 837
    implementation_ref: str  # e.g. 005010X279A1
    usage: str = "T"  # T = test, P = production


def seg(*elements: object, d: Delimiters = DEFAULT) -> str:
    """Join elements into a segment, dropping trailing empty elements (X12 rule)."""
    parts = ["" if e is None else str(e) for e in elements]
    while parts and parts[-1] == "":
        parts.pop()
    return d.element.join(parts)


def comp(*components: object, d: Delimiters = DEFAULT) -> str:
    parts = ["" if c is None else str(c) for c in components]
    while parts and parts[-1] == "":
        parts.pop()
    return d.component.join(parts)


def wrap_interchange(
    body_segments: list[str],
    transaction_set: str,
    env: EnvelopeConfig,
    control_number: int,
    ts: datetime,
    d: Delimiters = DEFAULT,
) -> str:
    """Wrap one transaction body (everything between ST and SE) in ST/SE, GS/GE and ISA/IEA."""
    st_control = f"{control_number % 10000:04d}"
    st = seg("ST", transaction_set, st_control, env.implementation_ref, d=d)
    se = seg("SE", len(body_segments) + 2, st_control, d=d)
    isa = d.element.join([
        "ISA", "00", " " * 10, "00", " " * 10,
        "ZZ", env.sender_id.ljust(15)[:15],
        "ZZ", env.receiver_id.ljust(15)[:15],
        ts.strftime("%y%m%d"), ts.strftime("%H%M"),
        d.repetition, "00501", f"{control_number:09d}", "0", env.usage, d.component,
    ])
    gs = seg("GS", env.functional_id, env.sender_id, env.receiver_id,
             ts.strftime("%Y%m%d"), ts.strftime("%H%M"), control_number, "X",
             env.implementation_ref, d=d)
    ge = seg("GE", 1, control_number, d=d)
    iea = seg("IEA", 1, f"{control_number:09d}", d=d)
    segments = [isa, gs, st, *body_segments, se, ge, iea]
    return (d.segment + "\n").join(segments) + d.segment + "\n"


def detect_delimiters(raw: str) -> Delimiters:
    text = raw.lstrip()
    if text.startswith("ISA") and len(text) >= 106:
        return Delimiters(element=text[3], repetition=text[82], component=text[104], segment=text[105])
    return DEFAULT


def parse_segments(raw: str) -> list[list[str]]:
    """Tokenize an X12 string into segments of elements, tolerating line breaks."""
    if not raw or not raw.strip():
        raise X12ParseError("Empty X12 payload")
    d = detect_delimiters(raw)
    segments = []
    for chunk in raw.split(d.segment):
        chunk = chunk.strip("\r\n\t ")
        if chunk:
            segments.append(chunk.split(d.element))
    return segments


def validate_envelope(segments: list[list[str]], transaction_set: str) -> None:
    """Check ST/SE counts and control numbers for every transaction set present."""
    ids = [s[0] for s in segments]
    if transaction_set not in [s[1] for s in segments if s[0] == "ST" and len(s) > 1]:
        raise X12ParseError(f"No ST*{transaction_set} transaction set found")
    start = None
    for i, s in enumerate(segments):
        if s[0] == "ST":
            start = i
        elif s[0] == "SE":
            if start is None:
                raise X12ParseError("SE without matching ST")
            count = i - start + 1
            if int(s[1]) != count:
                raise X12ParseError(f"SE01 segment count {s[1]} != actual {count}")
            if s[2] != segments[start][2]:
                raise X12ParseError("SE02 control number does not match ST02")
            start = None
    if start is not None:
        raise X12ParseError("ST without matching SE")
    if "ISA" in ids and "IEA" not in ids:
        raise X12ParseError("ISA without matching IEA")


def element(s: list[str], idx: int, default: str = "") -> str:
    return s[idx] if len(s) > idx else default
