"""
Database layer: SQLite with WAL mode.
Schema overview
---------------
leads               – one row per (workflow_id, canonical_phone_hash). PII encrypted.
lead_field_values   – EAV store for non-PII field values.
batches             – tracks ingested batch files (idempotency).
events              – append-only interaction ledger.
drops               – tracks ingested drop directories (idempotency).
rejections          – rejected lead rows.
orphan_events       – events that couldn't be linked to a lead at ingest time.
"""

import hashlib
import sqlite3
from pathlib import Path

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS drops (
    drop_path   TEXT PRIMARY KEY,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_key   TEXT PRIMARY KEY,          -- "<drop_path>|<workflow_id>|<batch_id>"
    drop_path   TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id      TEXT NOT NULL,
    phone_hash       TEXT NOT NULL,        -- SHA-256 of "+91XXXXXXXXXX"
    phone_enc        TEXT NOT NULL,        -- AES-128-CBC encrypted canonical phone (hex)
    phone_masked     TEXT NOT NULL,        -- "+91******XXXX"
    batch_id         TEXT NOT NULL,        -- first batch that created this lead
    external_ref     TEXT,                 -- reference field value (not PII)
    customer_name_enc TEXT,               -- encrypted customer name
    -- non-PII optional fields stored directly
    policy_expiry    TEXT,
    premium_amount   TEXT,
    preferred_language TEXT,
    product          TEXT,
    order_date       TEXT,
    -- metadata
    field_warnings   INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS rejections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    event_type      TEXT NOT NULL,
    workflow_id     TEXT NOT NULL,
    call_id         TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,          -- ISO-8601 UTC
    lead_id         INTEGER,                -- NULL if orphan at ingest time, resolved later
    phone_hash      TEXT,                   -- hash of normalised phone (NULL for overrides)
    payload         TEXT NOT NULL,          -- JSON string
    ingested_at     TEXT NOT NULL,
    FOREIGN KEY(lead_id) REFERENCES leads(id)
);

CREATE INDEX IF NOT EXISTS idx_events_call_id      ON events(call_id);
CREATE INDEX IF NOT EXISTS idx_events_lead_id      ON events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_phone_hash   ON events(phone_hash);
CREATE INDEX IF NOT EXISTS idx_events_event_type   ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_leads_workflow      ON leads(workflow_id);
"""


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    return con


def phone_hash(canonical_phone: str) -> str:
    """SHA-256 of the canonical phone string '+91XXXXXXXXXX'."""
    return hashlib.sha256(canonical_phone.encode()).hexdigest()
