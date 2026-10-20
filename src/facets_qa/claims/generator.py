"""Deterministic, PHI-safe synthetic test data (subscribers, providers, payers)."""

from __future__ import annotations

import random
from datetime import date, timedelta

from facets_qa.common.npi import generate_npi

VALID_EIN_PREFIXES = (20, 26, 27, 45, 46, 47, 81, 82, 83, 84, 85, 86, 87, 88)
FIRST = ["ALEX", "JORDAN", "TAYLOR", "MORGAN", "CASEY", "RILEY", "AVERY", "QUINN"]
LAST = ["TESTCASE", "SYNTHETIC", "QASAMPLE", "MOCKDATA", "FIXTURE", "NOTREAL"]


class SyntheticFactory:
    """Same seed -> same data, so scenario outputs and expected Facets rows are reproducible."""

    def __init__(self, seed: int = 837):
        self.rng = random.Random(seed)
        self._member_seq = 0
        self._provider_seq = 0

    def address(self) -> dict:
        return {"line1": f"{self.rng.randint(100, 9999)} TEST DATA WAY", "city": "SYNTHVILLE",
                "state": "TX", "zip": f"7{self.rng.randint(0, 9999):04d}"}

    def subscriber(self, min_age: int = 18, max_age: int = 80) -> dict:
        self._member_seq += 1
        age_days = self.rng.randint(min_age * 365, max_age * 365)
        return {
            "member_id": f"7{self._member_seq:08d}",  # 9 digits, 7xxxxxxxx range reserved for test data
            "first_name": self.rng.choice(FIRST),
            "last_name": self.rng.choice(LAST),
            "dob": date(2026, 1, 1) - timedelta(days=age_days),
            "gender": self.rng.choice("MF"),
            "address": self.address(),
        }

    def provider(self, organization: bool = True) -> dict:
        self._provider_seq += 1
        return {
            "name": f"QA {'MEDICAL GROUP' if organization else 'PROVIDER'} {self._provider_seq:03d}",
            "npi": generate_npi(self.rng, entity_type=2 if organization else 1),
            "tax_id": f"{self.rng.choice(VALID_EIN_PREFIXES)}{self.rng.randint(0, 9_999_999):07d}",
            "address": self.address(),
        }

    @staticmethod
    def payer() -> dict:
        return {"payer_id": "FACETS01", "name": "FACETS HEALTH PLAN"}
