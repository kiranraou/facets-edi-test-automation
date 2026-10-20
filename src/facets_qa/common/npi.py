"""NPI check-digit validation (Luhn over the 80840 health-industry prefix)."""

from __future__ import annotations

import random

NPI_PREFIX = "80840"


def npi_check_digit(base9: str) -> str:
    """Return the Luhn check digit for the first 9 digits of an NPI."""
    if len(base9) != 9 or not base9.isdigit():
        raise ValueError("NPI base must be exactly 9 digits")
    total = 0
    # Walk right-to-left; the rightmost payload digit is doubled because the
    # check digit will be appended to its right.
    for i, ch in enumerate(reversed(NPI_PREFIX + base9)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def is_valid_npi(npi: str) -> bool:
    if not isinstance(npi, str) or len(npi) != 10 or not npi.isdigit():
        return False
    if npi[0] not in "12":  # NPPES issues NPIs starting with 1 (individual) or 2 (organization)
        return False
    return npi_check_digit(npi[:9]) == npi[9]


def generate_npi(rng: random.Random, entity_type: int = 1) -> str:
    """Generate a syntactically valid (synthetic) NPI."""
    base = str(entity_type) + "".join(str(rng.randint(0, 9)) for _ in range(8))
    return base + npi_check_digit(base)
