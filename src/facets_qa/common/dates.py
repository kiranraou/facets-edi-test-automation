"""X12 date helpers (D8 = CCYYMMDD, RD8 = CCYYMMDD-CCYYMMDD) and Facets date conventions."""

from __future__ import annotations

from datetime import date, datetime

# Facets stores open-ended spans with a high-date term.
FACETS_HIGH_DATE = date(9999, 12, 31)


def format_d8(d: date) -> str:
    return d.strftime("%Y%m%d")


def parse_d8(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid D8 date: {value!r}") from exc


def format_rd8(start: date, end: date) -> str:
    return f"{format_d8(start)}-{format_d8(end)}"


def parse_rd8(value: str) -> tuple[date, date]:
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid RD8 range: {value!r}")
    start, end = parse_d8(parts[0]), parse_d8(parts[1])
    if end < start:
        raise ValueError(f"RD8 range ends before it starts: {value!r}")
    return start, end


def parse_x12_date(fmt: str, value: str) -> tuple[date, date]:
    """Parse a DTP date by its format qualifier; a D8 returns (d, d)."""
    if fmt == "D8":
        d = parse_d8(value)
        return d, d
    if fmt == "RD8":
        return parse_rd8(value)
    raise ValueError(f"Unsupported date format qualifier: {fmt!r}")


def to_date(value) -> date:
    """Coerce DB/Spark/pandas date-ish values to datetime.date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):  # pandas Timestamp
        return value.to_pydatetime().date()
    return date.fromisoformat(str(value)[:10])
