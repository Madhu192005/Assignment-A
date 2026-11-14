"""
Field normalisation and validation.

Implements all the type rules from the spec:
  - string
  - phone   → canonical "+91XXXXXXXXXX"
  - date    → "YYYY-MM-DD"
  - decimal → "X.XX"
  - enum    → lower-case matched value
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation


# ---------------------------------------------------------------------------
# Header normalisation
# ---------------------------------------------------------------------------

def normalise_header(h: str) -> str:
    """Lower-case, trim, collapse runs of _ or whitespace to a single space."""
    h = h.strip().lower()
    h = re.sub(r"[_\s]+", " ", h)
    return h


def match_field(raw_header: str, fields: list[dict]) -> dict | None:
    """Return the schema field whose name or alias matches raw_header, or None."""
    nh = normalise_header(raw_header)
    for field in fields:
        names = [field["name"]] + field.get("aliases", [])
        if nh in [normalise_header(n) for n in names]:
            return field
    return None


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

VALID_PREFIXES = set("6789")


def normalise_phone(raw: str) -> str | None:
    """Return canonical '+91XXXXXXXXXX' or None if invalid."""
    if not raw or not raw.strip():
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in VALID_PREFIXES:
        return None
    return f"+91{digits}"


# ---------------------------------------------------------------------------
# Date
# ---------------------------------------------------------------------------

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_PATTERNS = [
    # YYYY-MM-DD
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    # DD/MM/YYYY
    (re.compile(r"^(\d{2})/(\d{2})/(\d{4})$"), lambda m: (int(m[3]), int(m[2]), int(m[1]))),
    # DD-MM-YYYY
    (re.compile(r"^(\d{2})-(\d{2})-(\d{4})$"), lambda m: (int(m[3]), int(m[2]), int(m[1]))),
    # DD-Mon-YYYY  (case-insensitive month name)
    (re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$"),
     lambda m: (int(m[3]), MONTH_MAP.get(m[2].lower()), int(m[1]))),
]


def normalise_date(raw: str) -> str | None:
    """Return 'YYYY-MM-DD' or None if invalid / impossible date."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    for pattern, extractor in _DATE_PATTERNS:
        m = pattern.match(raw)
        if m:
            try:
                year, month, day = extractor(m)
                if month is None:
                    return None
                dt = datetime(year, month, day)
                return dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                return None
    return None


# ---------------------------------------------------------------------------
# Decimal
# ---------------------------------------------------------------------------

_CURRENCY_PREFIX = re.compile(
    r"^\s*(?:₹|rs\.?|inr)?\s*", re.IGNORECASE
)


def normalise_decimal(raw: str) -> str | None:
    """Return 'X.XX' string (rounded to 2 dp, ≥ 0) or None."""
    if not raw or not raw.strip():
        return None
    s = _CURRENCY_PREFIX.sub("", raw.strip())
    s = s.replace(",", "").strip()
    try:
        d = Decimal(s)
        if d < 0:
            return None
        return str(round(d, 2))
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------

def normalise_enum(raw: str, values: list[str]) -> str | None:
    """Case-insensitive match; return lower-case value or None."""
    if not raw or not raw.strip():
        return None
    low = raw.strip().lower()
    for v in values:
        if low == v.lower():
            return v.lower()
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def normalise_value(raw: str, field: dict) -> tuple[str | None, bool]:
    """
    Normalise raw string for the given field schema.

    Returns (normalised_value_or_None, is_valid).
    is_valid=False means the value was present but invalid.
    """
    ftype = field["type"]
    if ftype == "string":
        v = raw.strip() if raw else None
        return (v or None), True
    elif ftype == "phone":
        result = normalise_phone(raw)
        if raw and raw.strip() and result is None:
            return None, False
        return result, True
    elif ftype == "date":
        result = normalise_date(raw)
        if raw and raw.strip() and result is None:
            return None, False
        return result, True
    elif ftype == "decimal":
        result = normalise_decimal(raw)
        if raw and raw.strip() and result is None:
            return None, False
        return result, True
    elif ftype == "enum":
        result = normalise_enum(raw, field.get("values", []))
        if raw and raw.strip() and result is None:
            return None, False
        return result, True
    return raw.strip() or None, True
