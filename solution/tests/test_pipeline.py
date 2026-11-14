"""
Tests for the Interaction Ledger & Reporting Pipeline.

Covers: phone normalisation, date parsing, decimal parsing, enum normalisation,
        effective disposition logic, bucket logic, PII encryption, idempotency.

Run with: pytest solution/tests/ -v
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# normalise.py tests
# ---------------------------------------------------------------------------
from solution.normalise import (
    normalise_phone,
    normalise_date,
    normalise_decimal,
    normalise_enum,
    normalise_header,
    match_field,
)


class TestPhoneNormalisation:
    """Spec: strip non-digits; handle 91 prefix, 0 prefix; validate 10 digits + prefix 6-9."""

    def test_bare_10_digits(self):
        assert normalise_phone("9876543210") == "+919876543210"

    def test_plus91_space(self):
        assert normalise_phone("+91 98765 43210") == "+919876543210"

    def test_91_prefix_12_digits(self):
        assert normalise_phone("919876543210") == "+919876543210"

    def test_zero_prefix_11_digits(self):
        assert normalise_phone("09876543210") == "+919876543210"

    def test_91_dashes(self):
        assert normalise_phone("91-77187-50036") == "+917718750036"

    def test_plus91_no_space(self):
        assert normalise_phone("+919876543210") == "+919876543210"

    def test_in_parens(self):
        assert normalise_phone("(+91) 9876543210") == "+919876543210"

    def test_9_digits_invalid(self):
        assert normalise_phone("987654321") is None  # 9 digits

    def test_bad_prefix_5(self):
        assert normalise_phone("5876543210") is None  # starts with 5

    def test_landline_invalid(self):
        assert normalise_phone("022-2500-1234") is None

    def test_na_invalid(self):
        assert normalise_phone("N/A") is None

    def test_us_number_invalid(self):
        assert normalise_phone("+1 415 555 0134") is None

    def test_empty_string(self):
        assert normalise_phone("") is None

    def test_6_prefix_valid(self):
        assert normalise_phone("6876543210") == "+916876543210"

    def test_7_prefix_valid(self):
        assert normalise_phone("7876543210") == "+917876543210"


class TestDateNormalisation:
    """Spec: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY. Day-first. Impossible dates invalid."""

    def test_iso(self):
        assert normalise_date("2026-11-23") == "2026-11-23"

    def test_dd_slash_mm_yyyy(self):
        assert normalise_date("27/10/2026") == "2026-10-27"

    def test_dd_dash_mm_yyyy(self):
        assert normalise_date("11-12-2026") == "2026-12-11"

    def test_dd_mon_yyyy_lower(self):
        assert normalise_date("01-dec-2026") == "2026-12-01"

    def test_dd_mon_yyyy_mixed(self):
        assert normalise_date("21-Nov-2026") == "2026-11-21"

    def test_impossible_date(self):
        assert normalise_date("31/02/2026") is None

    def test_invalid_month_num(self):
        assert normalise_date("2026-13-01") is None

    def test_text_invalid(self):
        assert normalise_date("soon") is None

    def test_tbd_invalid(self):
        assert normalise_date("TBD") is None

    def test_empty(self):
        assert normalise_date("") is None


class TestDecimalNormalisation:
    """Spec: strip ₹/Rs/INR prefix, strip commas, must be ≥ 0, round to 2dp."""

    def test_plain_float(self):
        assert normalise_decimal("12345.67") == "12345.67"

    def test_int(self):
        assert normalise_decimal("12345") == "12345.00"

    def test_rupee_symbol_with_commas(self):
        result = normalise_decimal("₹1,27,554")
        assert result == "127554.00"

    def test_rs_dot_prefix(self):
        assert normalise_decimal("Rs. 25301") == "25301.00"

    def test_inr_prefix(self):
        assert normalise_decimal("INR 25301.4") == "25301.40"

    def test_rs_prefix(self):
        assert normalise_decimal("Rs 6850") == "6850.00"

    def test_negative_invalid(self):
        assert normalise_decimal("-100") is None

    def test_text_invalid(self):
        assert normalise_decimal("twelve thousand") is None

    def test_na_invalid(self):
        assert normalise_decimal("N/A") is None

    def test_empty(self):
        assert normalise_decimal("") is None

    def test_rounding(self):
        assert normalise_decimal("1.999") == "2.00"

    def test_zero(self):
        assert normalise_decimal("0") == "0.00"


class TestEnumNormalisation:
    VALUES = ["en", "hi", "ta", "kn", "te", "mr"]

    def test_exact(self):
        assert normalise_enum("en", self.VALUES) == "en"

    def test_upper(self):
        assert normalise_enum("EN", self.VALUES) == "en"

    def test_mixed(self):
        assert normalise_enum("Hi", self.VALUES) == "hi"

    def test_invalid(self):
        assert normalise_enum("Hindi", self.VALUES) is None

    def test_empty(self):
        assert normalise_enum("", self.VALUES) is None


class TestHeaderNormalisation:
    def test_basic(self):
        assert normalise_header("customer_name") == "customer name"

    def test_mixed_case_spaces(self):
        assert normalise_header("  Mobile No  ") == "mobile no"

    def test_multiple_underscores(self):
        assert normalise_header("policy__number") == "policy number"

    def test_alias_match(self):
        field = {"name": "phone", "aliases": ["mobile", "mobile no", "phone number", "contact"]}
        assert match_field("Mobile No", [field])["name"] == "phone"

    def test_no_match(self):
        field = {"name": "phone", "aliases": ["mobile"]}
        assert match_field("agent_notes", [field]) is None


# ---------------------------------------------------------------------------
# crypto.py tests
# ---------------------------------------------------------------------------
from solution.crypto import encrypt, decrypt


class TestCrypto:
    def test_roundtrip(self):
        plain = "+919876543210"
        assert decrypt(encrypt(plain)) == plain

    def test_different_iv_each_time(self):
        plain = "test"
        t1 = encrypt(plain)
        t2 = encrypt(plain)
        assert t1 != t2  # different IVs

    def test_decrypt_gives_original(self):
        for text in ["Aarav Sharma", "+919876543210", "MOT/2026/100000"]:
            assert decrypt(encrypt(text)) == text


# ---------------------------------------------------------------------------
# Bucket logic tests  (uses a minimal in-memory DB)
# ---------------------------------------------------------------------------
from solution.db import get_connection, DDL
from solution.reports import _compute_bucket, _get_calls_for_lead


def _make_test_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    return con


DISPOSITIONS = {
    "motor_renewal": {
        "renewed": {"is_terminal": True, "is_success": True},
        "not_interested": {"is_terminal": True, "is_success": False},
        "callback_requested": {"is_terminal": False, "is_success": False},
        "dnd": {"is_terminal": True, "is_success": False},
    }
}


def _insert_lead(con, lead_id=1, workflow_id="motor_renewal"):
    con.execute(
        """INSERT INTO leads(id, workflow_id, phone_hash, phone_enc, phone_masked,
           batch_id, field_warnings, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (lead_id, workflow_id, "hash1", "enc", "+91******1234",
         "batch_001", 0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )


def _insert_event(con, event_id, event_type, call_id, lead_id=1,
                   workflow_id="motor_renewal", occurred_at="2026-10-01T10:00:00Z",
                   payload=None):
    con.execute(
        """INSERT INTO events(event_id, event_type, workflow_id, call_id,
           occurred_at_utc, lead_id, phone_hash, payload, ingested_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (event_id, event_type, workflow_id, call_id,
         occurred_at, lead_id, "hash1",
         json.dumps(payload or {}), "2026-01-01T00:00:00Z"),
    )


class TestBucketLogic:
    def test_no_calls_yet_to_reach_out(self):
        con = _make_test_db()
        _insert_lead(con)
        calls = _get_calls_for_lead(con, 1)
        bucket = _compute_bucket(calls, con, 1, DISPOSITIONS["motor_renewal"])
        assert bucket == "yet_to_reach_out"

    def test_terminal_disposition_completed(self):
        con = _make_test_db()
        _insert_lead(con)
        _insert_event(con, "e1", "call.initiated", "c1", occurred_at="2026-10-01T10:00:00Z")
        _insert_event(con, "e2", "call.connected", "c1", occurred_at="2026-10-01T10:00:10Z")
        _insert_event(con, "e3", "disposition.set", "c1",
                      payload={"disposition_code": "renewed"},
                      occurred_at="2026-10-01T10:05:00Z")
        calls = _get_calls_for_lead(con, 1)
        bucket = _compute_bucket(calls, con, 1, DISPOSITIONS["motor_renewal"])
        assert bucket == "completed"

    def test_all_telephony_failed(self):
        con = _make_test_db()
        _insert_lead(con)
        _insert_event(con, "e1", "call.initiated", "c1", occurred_at="2026-10-01T10:00:00Z")
        _insert_event(con, "e2", "call.telephony_failed", "c1", occurred_at="2026-10-01T10:00:05Z")
        calls = _get_calls_for_lead(con, 1)
        bucket = _compute_bucket(calls, con, 1, DISPOSITIONS["motor_renewal"])
        assert bucket == "in_progress_telephony_failed"

    def test_some_calls_no_terminal_in_progress(self):
        con = _make_test_db()
        _insert_lead(con)
        _insert_event(con, "e1", "call.initiated", "c1", occurred_at="2026-10-01T10:00:00Z")
        _insert_event(con, "e2", "call.connected", "c1", occurred_at="2026-10-01T10:00:10Z")
        _insert_event(con, "e3", "disposition.set", "c1",
                      payload={"disposition_code": "callback_requested"},
                      occurred_at="2026-10-01T10:05:00Z")
        calls = _get_calls_for_lead(con, 1)
        bucket = _compute_bucket(calls, con, 1, DISPOSITIONS["motor_renewal"])
        assert bucket == "in_progress_dialed"

    def test_override_wins_over_set(self):
        """disposition.override should take precedence over disposition.set."""
        con = _make_test_db()
        _insert_lead(con)
        _insert_event(con, "e1", "call.initiated", "c1", occurred_at="2026-10-01T10:00:00Z")
        _insert_event(con, "e2", "call.connected", "c1", occurred_at="2026-10-01T10:00:10Z")
        _insert_event(con, "e3", "disposition.set", "c1",
                      payload={"disposition_code": "callback_requested"},
                      occurred_at="2026-10-01T10:05:00Z")
        # Override changes to terminal
        _insert_event(con, "e4", "disposition.override", "c1",
                      payload={"disposition_code": "renewed",
                               "overridden_by": "supervisor_1", "reason": "qa_review"},
                      occurred_at="2026-10-01T12:00:00Z")
        calls = _get_calls_for_lead(con, 1)
        bucket = _compute_bucket(calls, con, 1, DISPOSITIONS["motor_renewal"])
        assert bucket == "completed"

    def test_mixed_calls_one_telephony_failed_one_ok(self):
        """If not ALL calls have telephony_failed, bucket is in_progress_dialed."""
        con = _make_test_db()
        _insert_lead(con)
        _insert_event(con, "e1", "call.initiated", "c1", occurred_at="2026-10-01T10:00:00Z")
        _insert_event(con, "e2", "call.telephony_failed", "c1", occurred_at="2026-10-01T10:00:05Z")
        _insert_event(con, "e3", "call.initiated", "c2", occurred_at="2026-10-01T11:00:00Z")
        _insert_event(con, "e4", "call.connected", "c2", occurred_at="2026-10-01T11:00:15Z")
        calls = _get_calls_for_lead(con, 1)
        bucket = _compute_bucket(calls, con, 1, DISPOSITIONS["motor_renewal"])
        assert bucket == "in_progress_dialed"


# ---------------------------------------------------------------------------
# Effective disposition: latest override wins
# ---------------------------------------------------------------------------
from solution.reports import _get_effective_disposition


class TestEffectiveDisposition:
    def test_no_disposition(self):
        con = _make_test_db()
        _insert_lead(con)
        _insert_event(con, "e1", "call.initiated", "c1")
        result = _get_effective_disposition(con, "c1", 1, DISPOSITIONS["motor_renewal"])
        assert result is None

    def test_single_set(self):
        con = _make_test_db()
        _insert_lead(con)
        _insert_event(con, "e1", "disposition.set", "c1",
                      payload={"disposition_code": "renewed"},
                      occurred_at="2026-10-01T10:00:00Z")
        result = _get_effective_disposition(con, "c1", 1, DISPOSITIONS["motor_renewal"])
        assert result == "renewed"

    def test_override_beats_set(self):
        con = _make_test_db()
        _insert_lead(con)
        _insert_event(con, "e1", "disposition.set", "c1",
                      payload={"disposition_code": "callback_requested"},
                      occurred_at="2026-10-01T10:00:00Z")
        _insert_event(con, "e2", "disposition.override", "c1",
                      payload={"disposition_code": "dnd",
                               "overridden_by": "supervisor_1", "reason": "qa_review"},
                      occurred_at="2026-10-01T12:00:00Z")
        result = _get_effective_disposition(con, "c1", 1, DISPOSITIONS["motor_renewal"])
        assert result == "dnd"

    def test_latest_override_wins(self):
        con = _make_test_db()
        _insert_lead(con)
        _insert_event(con, "e1", "disposition.override", "c1",
                      payload={"disposition_code": "callback_requested",
                               "overridden_by": "supervisor_1", "reason": "qa_review"},
                      occurred_at="2026-10-01T10:00:00Z")
        _insert_event(con, "e2", "disposition.override", "c1",
                      payload={"disposition_code": "renewed",
                               "overridden_by": "supervisor_2", "reason": "qa_review"},
                      occurred_at="2026-10-01T14:00:00Z")  # later
        result = _get_effective_disposition(con, "c1", 1, DISPOSITIONS["motor_renewal"])
        assert result == "renewed"


# ---------------------------------------------------------------------------
# Timestamp parsing tests
# ---------------------------------------------------------------------------
from solution.ingest_events import parse_timestamp
from datetime import timezone


class TestTimestampParsing:
    def test_utc_z(self):
        dt = parse_timestamp("2026-10-05T13:20:21Z")
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 13

    def test_offset_ist(self):
        dt = parse_timestamp("2026-10-01T10:40:19+05:30")
        # Should be UTC: 10:40:19 - 5:30 = 05:10:19
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 5
        assert dt.minute == 10

    def test_naive_treated_as_ist(self):
        dt = parse_timestamp("2026-10-03 10:01:00")
        # IST 10:01 → UTC 04:31
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 4
        assert dt.minute == 31
