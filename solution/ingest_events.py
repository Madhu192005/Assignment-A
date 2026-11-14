"""
Event ingestion: reads JSONL event files, deduplicates, links to leads,
quarantines orphans, and writes to the append-only events ledger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import phone_hash
from .normalise import normalise_phone

IST = timezone(timedelta(hours=5, minutes=30))


def parse_timestamp(s: str) -> datetime:
    """
    Parse event timestamp in three forms:
      - UTC:    '...Z'
      - offset: '...+05:30' (or other offsets)
      - naive:  '2026-10-03 10:01:00' → assume IST
    Returns a timezone-aware UTC datetime.
    """
    s = s.strip()
    if s.endswith("Z"):
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=timezone.utc)
    if "T" in s and ("+" in s[10:] or "-" in s[10:]):
        # fromisoformat handles +HH:MM offsets in Py 3.11+
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    # Naive → IST
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=IST).astimezone(timezone.utc)


def to_utc_iso(s: str) -> str:
    return parse_timestamp(s).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lookup_lead(con: sqlite3.Connection, workflow_id: str, phone_hash_val: str) -> int | None:
    row = con.execute(
        "SELECT id FROM leads WHERE workflow_id=? AND phone_hash=?",
        (workflow_id, phone_hash_val),
    ).fetchone()
    return row["id"] if row else None


def _resolve_override_lead(con: sqlite3.Connection, call_id: str) -> int | None:
    """
    For disposition.override (which has no phone), look up the lead through
    any other event that shares the call_id.
    """
    row = con.execute(
        "SELECT lead_id FROM events WHERE call_id=? AND lead_id IS NOT NULL LIMIT 1",
        (call_id,),
    ).fetchone()
    return row["lead_id"] if row else None


def _resolve_orphan_overrides(con: sqlite3.Connection) -> int:
    """
    Re-attempt to link previously-orphaned disposition.override events
    after new leads have been ingested.
    Returns number newly resolved.
    """
    orphans = con.execute(
        """
        SELECT id, call_id FROM events
        WHERE lead_id IS NULL AND event_type = 'disposition.override'
        """
    ).fetchall()
    resolved = 0
    for row in orphans:
        lead_id = _resolve_override_lead(con, row["call_id"])
        if lead_id is not None:
            con.execute("UPDATE events SET lead_id=? WHERE id=?", (lead_id, row["id"]))
            resolved += 1
    return resolved


def _resolve_orphan_phone_events(con: sqlite3.Connection) -> int:
    """
    Re-attempt to link previously-orphaned non-override events (those whose
    lead arrived in a later drop) now that new leads exist.
    Returns number newly resolved.
    """
    orphans = con.execute(
        """
        SELECT id, workflow_id, phone_hash FROM events
        WHERE lead_id IS NULL AND phone_hash IS NOT NULL
        """
    ).fetchall()
    resolved = 0
    for row in orphans:
        lead_id = _lookup_lead(con, row["workflow_id"], row["phone_hash"])
        if lead_id is not None:
            con.execute("UPDATE events SET lead_id=? WHERE id=?", (lead_id, row["id"]))
            resolved += 1
    return resolved


def ingest_events(
    drop_path: Path,
    con: sqlite3.Connection,
    dispositions: dict[str, dict[str, dict]],
    drop_key: str,
) -> dict:
    """
    Ingest all JSONL event files from drop_path/events/.
    Returns stats dict.
    """
    events_root = drop_path / "events"
    if not events_root.exists():
        return {}

    stats = {
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "orphan_events": 0,
        "unknown_disposition_events": 0,
    }

    now = datetime.now(timezone.utc).isoformat()

    for jsonl_file in sorted(events_root.glob("*.jsonl")):
        with open(jsonl_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                stats["events_total"] += 1

                # 1. Malformed?
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    stats["malformed_events"] += 1
                    continue
                if not isinstance(evt, dict) or not evt.get("event_id"):
                    stats["malformed_events"] += 1
                    continue

                event_id = evt["event_id"]
                event_type = evt.get("event_type", "")
                workflow_id = evt.get("workflow_id", "")
                call_id = evt.get("call_id", "")
                occurred_at_raw = evt.get("occurred_at", "")
                phone_raw = evt.get("phone")
                payload = json.dumps(evt.get("payload") or {})

                # 2. Duplicate?
                existing = con.execute(
                    "SELECT 1 FROM events WHERE event_id=?", (event_id,)
                ).fetchone()
                if existing:
                    stats["duplicate_events_dropped"] += 1
                    continue

                # Parse timestamp
                try:
                    occurred_at_utc = to_utc_iso(occurred_at_raw)
                except (ValueError, TypeError):
                    stats["malformed_events"] += 1
                    continue

                # 3. Unknown disposition?
                if event_type in ("disposition.set", "disposition.override"):
                    disp_code = (evt.get("payload") or {}).get("disposition_code", "")
                    wf_disps = dispositions.get(workflow_id, {})
                    if disp_code not in wf_disps:
                        stats["unknown_disposition_events"] += 1
                        # Still store it as unknown so it doesn't become orphan count
                        # but do NOT link it to reports (it won't pass disposition lookup)
                        # Per spec: count and ignore — we skip insertion entirely.
                        continue

                # 4. Link to lead
                lead_id: int | None = None
                ph_hash: str | None = None

                if event_type == "disposition.override":
                    # No phone; link via call_id
                    lead_id = _resolve_override_lead(con, call_id)
                else:
                    if phone_raw:
                        canonical = normalise_phone(str(phone_raw))
                        if canonical:
                            ph_hash = phone_hash(canonical)
                            lead_id = _lookup_lead(con, workflow_id, ph_hash)

                if lead_id is None:
                    stats["orphan_events"] += 1

                # Insert into ledger
                con.execute(
                    """
                    INSERT INTO events(
                        event_id, event_type, workflow_id, call_id,
                        occurred_at_utc, lead_id, phone_hash, payload, ingested_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event_id, event_type, workflow_id, call_id,
                        occurred_at_utc, lead_id, ph_hash, payload, now,
                    ),
                )

    # After inserting, resolve any previously-orphaned events that can now be linked
    _resolve_orphan_phone_events(con)
    _resolve_orphan_overrides(con)

    return stats
