"""
Lead ingestion: reads CSV files, validates and normalises rows,
merges duplicates, stores leads in the DB.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .crypto import encrypt
from .db import phone_hash
from .normalise import match_field, normalise_phone, normalise_value


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_schemas(schemas_dir: Path) -> dict[str, dict]:
    """Return {workflow_id: schema_dict}."""
    schemas = {}
    for p in schemas_dir.glob("*.json"):
        schema = json.loads(p.read_text(encoding="utf-8"))
        schemas[schema["workflow_id"]] = schema
    return schemas


def load_dispositions(schemas_dir: Path) -> dict[str, dict[str, dict]]:
    """Return {workflow_id: {code: {is_terminal, is_success}}}."""
    path = schemas_dir / "dispositions.csv"
    result: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            wf = row["workflow_id"]
            result.setdefault(wf, {})[row["code"]] = {
                "label": row["label"],
                "is_terminal": row["is_terminal"].strip().lower() == "true",
                "is_success": row["is_success"].strip().lower() == "true",
            }
    return result


def _is_blank_row(row: dict) -> bool:
    return all(not v or not v.strip() for v in row.values())


def _build_header_map(csv_headers: list[str], fields: list[dict]) -> dict[str, dict]:
    """Map csv column name → schema field (or absent if unrecognised)."""
    mapping = {}
    for h in csv_headers:
        field = match_field(h, fields)
        if field:
            mapping[h] = field
    return mapping


def _process_lead_row(
    row: dict,
    header_map: dict[str, dict],
    fields: list[dict],
    workflow_id: str,
) -> tuple[dict[str, Any] | None, str | None, int]:
    """
    Validate and normalise one CSV row.

    Returns:
        (values_dict, rejection_reason, field_warnings)
        If rejection_reason is not None, the row is rejected.
    """
    # Gather values keyed by field name
    field_values: dict[str, str] = {}
    for col_header, raw_val in row.items():
        field = header_map.get(col_header)
        if field is None:
            continue
        field_values[field["name"]] = raw_val or ""

    # Walk fields in schema order
    normalised: dict[str, Any] = {}
    field_warnings = 0

    for field in fields:
        fname = field["name"]
        required = field.get("required", False)
        raw = field_values.get(fname, "").strip()

        if not raw:
            if required:
                return None, f"missing_required:{fname}", 0
            normalised[fname] = None
            continue

        value, is_valid = normalise_value(raw, field)

        if not is_valid:
            # Phone present-but-invalid is a rejection trigger
            if field["type"] == "phone":
                return None, "invalid_phone", 0
            # Optional field present but invalid → store null, count warning
            if not required:
                normalised[fname] = None
                field_warnings += 1
                continue
            # Required field present but invalid (non-phone) → reject with missing
            return None, f"missing_required:{fname}", 0

        normalised[fname] = value

    return normalised, None, field_warnings


def ingest_leads(
    drop_path: Path,
    con: sqlite3.Connection,
    schemas: dict[str, dict],
    drop_key: str,
) -> dict:
    """
    Ingest all lead CSV files from drop_path/leads/.
    Returns stats dict.
    """
    leads_root = drop_path / "leads"
    if not leads_root.exists():
        return {}

    stats = {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "rejections_by_reason": {},
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
    }
    now = _now_utc()

    for wf_dir in sorted(leads_root.iterdir()):
        workflow_id = wf_dir.name
        if workflow_id not in schemas:
            continue
        schema = schemas[workflow_id]
        fields = schema["fields"]

        # Reference field (external_ref)
        ref_field = next((f for f in fields if f.get("reference")), None)
        # Defaults
        defaults = {f["name"]: f["default"] for f in fields if "default" in f}

        for csv_file in sorted(wf_dir.glob("*.csv")):
            batch_id = csv_file.stem
            batch_key = f"{drop_key}|{workflow_id}|{batch_id}"

            # Idempotency: skip already-ingested batches
            existing = con.execute(
                "SELECT 1 FROM batches WHERE batch_key=?", (batch_key,)
            ).fetchone()
            if existing:
                continue

            with open(csv_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                csv_headers = reader.fieldnames or []
                header_map = _build_header_map(csv_headers, fields)
                row_number = 0

                for raw_row in reader:
                    if _is_blank_row(raw_row):
                        continue
                    row_number += 1
                    stats["lead_rows_total"] += 1

                    normalised, rejection_reason, fwarnings = _process_lead_row(
                        raw_row, header_map, fields, workflow_id
                    )

                    if rejection_reason:
                        stats["lead_rows_rejected"] += 1
                        stats["rejections_by_reason"][rejection_reason] = (
                            stats["rejections_by_reason"].get(rejection_reason, 0) + 1
                        )
                        con.execute(
                            "INSERT INTO rejections(workflow_id, batch_id, row_number, reason) "
                            "VALUES (?,?,?,?)",
                            (workflow_id, batch_id, row_number, rejection_reason),
                        )
                        continue

                    stats["field_warnings"] += fwarnings

                    # Apply defaults
                    for fname, default_val in defaults.items():
                        if normalised.get(fname) is None:
                            normalised[fname] = default_val

                    canonical_phone = normalised.get("phone")
                    if canonical_phone is None:
                        # Phone required; would have been caught above, but guard anyway
                        continue

                    ph = phone_hash(canonical_phone)
                    phone_masked = "+91******" + canonical_phone[-4:]

                    # Encrypt PII
                    phone_enc = encrypt(canonical_phone)
                    name_enc = encrypt(normalised.get("customer_name") or "")
                    external_ref = normalised.get(ref_field["name"]) if ref_field else None

                    existing_lead = con.execute(
                        "SELECT id, batch_id FROM leads WHERE workflow_id=? AND phone_hash=?",
                        (workflow_id, ph),
                    ).fetchone()

                    if existing_lead is None:
                        # New lead
                        con.execute(
                            """
                            INSERT INTO leads(
                                workflow_id, phone_hash, phone_enc, phone_masked,
                                batch_id, external_ref,
                                customer_name_enc,
                                policy_expiry, premium_amount, preferred_language,
                                product, order_date,
                                field_warnings, created_at, updated_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                workflow_id, ph, phone_enc, phone_masked,
                                batch_id, external_ref,
                                name_enc,
                                normalised.get("policy_expiry"),
                                normalised.get("premium_amount"),
                                normalised.get("preferred_language"),
                                normalised.get("product"),
                                normalised.get("order_date"),
                                fwarnings, now, now,
                            ),
                        )
                    else:
                        # Duplicate: merge (last-write-wins for non-null values)
                        stats["duplicate_lead_rows_merged"] += 1
                        lead_id = existing_lead["id"]
                        updates: list[tuple] = []

                        def _maybe(col, val):
                            if val is not None:
                                updates.append((col, val))

                        _maybe("external_ref", external_ref)
                        _maybe("customer_name_enc", name_enc if normalised.get("customer_name") else None)
                        _maybe("policy_expiry", normalised.get("policy_expiry"))
                        _maybe("premium_amount", normalised.get("premium_amount"))
                        _maybe("preferred_language", normalised.get("preferred_language"))
                        _maybe("product", normalised.get("product"))
                        _maybe("order_date", normalised.get("order_date"))

                        for col, val in updates:
                            con.execute(
                                f"UPDATE leads SET {col}=?, field_warnings=field_warnings+?, updated_at=? WHERE id=?",
                                (val, fwarnings, now, lead_id),
                            )
                        if not updates:
                            con.execute(
                                "UPDATE leads SET updated_at=? WHERE id=?", (now, lead_id)
                            )

            # Mark batch as ingested
            con.execute(
                "INSERT INTO batches(batch_key, drop_path, workflow_id, batch_id, ingested_at) VALUES (?,?,?,?,?)",
                (batch_key, drop_key, workflow_id, batch_id, now),
            )

    return stats
