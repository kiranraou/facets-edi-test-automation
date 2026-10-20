from __future__ import annotations

import random
from datetime import date

import pytest

from facets_qa.common.dates import parse_rd8, parse_x12_date
from facets_qa.common.npi import generate_npi, is_valid_npi, npi_check_digit
from facets_qa.common.retry import (
    GatewayTimeout, RetryExhausted, RetryPolicy, call_with_retry,
)
from facets_qa.common.x12 import (
    EnvelopeConfig, X12ParseError, detect_delimiters, parse_segments, seg, validate_envelope, wrap_interchange,
)


# --------------------------------------------------------------------- NPI
@pytest.mark.contracts
@pytest.mark.parametrize("npi,valid", [
    ("1234567893", True),    # CMS published example
    ("1234567890", False),   # wrong check digit
    ("123456789", False),    # 9 digits
    ("12345678931", False),  # 11 digits
    ("3234567893", False),   # must start with 1 or 2
    ("12345A7893", False),
])
def test_npi_validation(npi, valid):
    assert is_valid_npi(npi) is valid


@pytest.mark.contracts
def test_npi_check_digit_matches_cms_example():
    assert npi_check_digit("123456789") == "3"


@pytest.mark.contracts
def test_generated_npis_are_valid():
    rng = random.Random(1)
    assert all(is_valid_npi(generate_npi(rng, entity_type=t)) for t in (1, 2) for _ in range(200))


# --------------------------------------------------------------------- dates
@pytest.mark.parser
def test_rd8_and_d8_parsing():
    assert parse_rd8("20250101-20251231") == (date(2025, 1, 1), date(2025, 12, 31))
    assert parse_x12_date("D8", "20250315") == (date(2025, 3, 15), date(2025, 3, 15))
    with pytest.raises(ValueError):
        parse_rd8("20251231-20250101")
    with pytest.raises(ValueError):
        parse_x12_date("DT", "202503151200")


# --------------------------------------------------------------------- X12
ENV = EnvelopeConfig("SENDER", "RECEIVER", "HS", "005010X279A1")


@pytest.mark.x12
def test_seg_drops_trailing_empty_elements():
    assert seg("NM1", "IL", 1, "DOE", "", "", None) == "NM1*IL*1*DOE"
    assert seg("HL", 1, "", 20, 1) == "HL*1**20*1"


@pytest.mark.x12
def test_interchange_envelope_counts_and_isa_width(service_date):
    from datetime import datetime
    raw = wrap_interchange([seg("BHT", "0022", "13"), seg("HL", 1, "", 20, 1)], "270", ENV, 42,
                           datetime(2026, 1, 2, 3, 4))
    isa = raw.split("~")[0]
    assert len(isa) == 105  # ISA is fixed width; terminator is the 106th char
    segments = parse_segments(raw)
    validate_envelope(segments, "270")
    se = next(s for s in segments if s[0] == "SE")
    assert se == ["SE", "4", "0042"]
    assert detect_delimiters(raw).component == ":"


@pytest.mark.x12
def test_validate_envelope_rejects_wrong_segment_count():
    segments = parse_segments("ST*270*0001~BHT*0022~SE*9*0001~")
    with pytest.raises(X12ParseError, match="segment count"):
        validate_envelope(segments, "270")


@pytest.mark.x12
def test_parse_segments_rejects_empty_payload():
    with pytest.raises(X12ParseError):
        parse_segments("   ")


# --------------------------------------------------------------------- retry
@pytest.mark.retry
def test_retry_succeeds_after_transient_failures(no_sleep):
    attempts = iter([GatewayTimeout("t1"), GatewayTimeout("t2"), "OK"])

    def flaky():
        v = next(attempts)
        if isinstance(v, Exception):
            raise v
        return v

    out = call_with_retry(flaky, RetryPolicy(max_attempts=4, base_delay=1, jitter=0), sleep=no_sleep)
    assert (out.value, out.attempts) == ("OK", 3)
    assert no_sleep.calls == [1, 2]  # exponential: base * 2^(n-1)


@pytest.mark.retry
def test_retry_gives_up_after_max_attempts(no_sleep):
    def always_down():
        raise GatewayTimeout("down")

    with pytest.raises(RetryExhausted) as exc:
        call_with_retry(always_down, RetryPolicy(max_attempts=3, base_delay=0.5, jitter=0), sleep=no_sleep)
    assert exc.value.attempts == 3
    assert no_sleep.calls == [0.5, 1.0]


@pytest.mark.retry
def test_non_transient_errors_are_not_retried(no_sleep):
    def broken():
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        call_with_retry(broken, RetryPolicy(), sleep=no_sleep)
    assert no_sleep.calls == []


@pytest.mark.retry
@pytest.mark.parametrize("attempt,expected", [(1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0), (5, 8.0), (6, 8.0)])
def test_backoff_is_exponential_and_capped(attempt, expected):
    assert RetryPolicy(base_delay=0.5, max_delay=8.0, jitter=0).delay_for(attempt) == expected


@pytest.mark.retry
def test_jitter_stays_within_bounds():
    rng, policy = random.Random(3), RetryPolicy(base_delay=1.0, jitter=0.1)
    delays = [policy.delay_for(1, rng) for _ in range(500)]
    assert min(delays) >= 0.9 and max(delays) <= 1.1
