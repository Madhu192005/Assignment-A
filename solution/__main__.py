"""
Main CLI entry point for the Interaction Ledger & Reporting Pipeline.

Usage:
    python -m solution ingest <drop_path> [--db DB_PATH] [--schemas SCHEMAS_DIR]
    python -m solution report --out <reports_dir> [--db DB_PATH] [--schemas SCHEMAS_DIR]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent.resolve()
REPO_ROOT = HERE.parent


def _resolve_schemas() -> Path:
    candidates = [
        REPO_ROOT / "assignment-a-data" / "schemas",
        REPO_ROOT / "schemas",
        Path.cwd() / "assignment-a-data" / "schemas",
        Path.cwd() / "schemas",
    ]
    for c in candidates:
        if c.exists() and (c / "dispositions.csv").exists():
            return c
    return REPO_ROOT / "assignment-a-data" / "schemas"


DEFAULT_SCHEMAS = _resolve_schemas()
DEFAULT_DB = Path.cwd() / "ledger.db"


def _ensure_dq_table(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS _dq_stats (
            id                          INTEGER PRIMARY KEY CHECK(id=1),
            lead_rows_total             INTEGER NOT NULL DEFAULT 0,
            lead_rows_rejected          INTEGER NOT NULL DEFAULT 0,
            rejections_by_reason        TEXT    NOT NULL DEFAULT '{}',
            duplicate_lead_rows_merged  INTEGER NOT NULL DEFAULT 0,
            field_warnings              INTEGER NOT NULL DEFAULT 0,
            events_total                INTEGER NOT NULL DEFAULT 0,
            malformed_events            INTEGER NOT NULL DEFAULT 0,
            duplicate_events_dropped    INTEGER NOT NULL DEFAULT 0,
            orphan_events               INTEGER NOT NULL DEFAULT 0,
            unknown_disposition_events  INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO _dq_stats(id) VALUES(1);
        """
    )


def _accumulate_stats(con: sqlite3.Connection, lead_stats: dict, event_stats: dict) -> None:
    _ensure_dq_table(con)
    row = con.execute("SELECT * FROM _dq_stats WHERE id=1").fetchone()
    old_rbt = json.loads(row["rejections_by_reason"]) if row else {}
    new_rbt = dict(old_rbt)
    for reason, count in lead_stats.get("rejections_by_reason", {}).items():
        new_rbt[reason] = new_rbt.get(reason, 0) + count

    con.execute(
        """
        UPDATE _dq_stats SET
            lead_rows_total            = lead_rows_total            + ?,
            lead_rows_rejected         = lead_rows_rejected         + ?,
            rejections_by_reason       = ?,
            duplicate_lead_rows_merged = duplicate_lead_rows_merged + ?,
            field_warnings             = field_warnings             + ?,
            events_total               = events_total               + ?,
            malformed_events           = malformed_events           + ?,
            duplicate_events_dropped   = duplicate_events_dropped   + ?,
            orphan_events              = orphan_events              + ?,
            unknown_disposition_events = unknown_disposition_events + ?
        WHERE id=1
        """,
        (
            lead_stats.get("lead_rows_total", 0),
            lead_stats.get("lead_rows_rejected", 0),
            json.dumps(new_rbt),
            lead_stats.get("duplicate_lead_rows_merged", 0),
            lead_stats.get("field_warnings", 0),
            event_stats.get("events_total", 0),
            event_stats.get("malformed_events", 0),
            event_stats.get("duplicate_events_dropped", 0),
            event_stats.get("orphan_events", 0),
            event_stats.get("unknown_disposition_events", 0),
        ),
    )


def cmd_ingest(args: argparse.Namespace) -> None:
    from .db import get_connection
    from .ingest_leads import load_schemas, load_dispositions, ingest_leads
    from .ingest_events import ingest_events

    drop_path = Path(args.drop_path).resolve()
    db_path = Path(args.db).resolve()
    schemas_dir = Path(args.schemas).resolve()

    if not drop_path.exists():
        print(f"ERROR: drop path does not exist: {drop_path}", file=sys.stderr)
        sys.exit(1)

    con = get_connection(db_path)
    _ensure_dq_table(con)

    drop_key = str(drop_path)

    # Idempotency: check if this drop was already fully ingested
    existing_drop = con.execute(
        "SELECT 1 FROM drops WHERE drop_path=?", (drop_key,)
    ).fetchone()
    if existing_drop:
        print(f"Drop already ingested (idempotent): {drop_path}")
        con.close()
        return

    schemas = load_schemas(schemas_dir)
    dispositions = load_dispositions(schemas_dir)

    print(f"Ingesting leads from {drop_path} ...")
    with con:
        lead_stats = ingest_leads(drop_path, con, schemas, drop_key)

    print(f"Ingesting events from {drop_path} ...")
    with con:
        event_stats = ingest_events(drop_path, con, dispositions, drop_key)

    with con:
        _accumulate_stats(con, lead_stats, event_stats)
        con.execute(
            "INSERT INTO drops(drop_path, ingested_at) VALUES(?,?)",
            (drop_key, datetime.now(timezone.utc).isoformat()),
        )

    # Recompute orphan count from live data (overrides may now be linked)
    orphans_live = con.execute(
        "SELECT COUNT(*) FROM events WHERE lead_id IS NULL"
    ).fetchone()[0]
    # Update orphan count to reflect resolved events
    with con:
        con.execute(
            "UPDATE _dq_stats SET orphan_events = ? WHERE id=1",
            (orphans_live,),
        )

    print(f"  Leads:  total={lead_stats.get('lead_rows_total',0)}, "
          f"rejected={lead_stats.get('lead_rows_rejected',0)}, "
          f"merged={lead_stats.get('duplicate_lead_rows_merged',0)}, "
          f"warnings={lead_stats.get('field_warnings',0)}")
    print(f"  Events: total={event_stats.get('events_total',0)}, "
          f"malformed={event_stats.get('malformed_events',0)}, "
          f"duplicates={event_stats.get('duplicate_events_dropped',0)}, "
          f"orphans={orphans_live}, "
          f"unknown_disposition={event_stats.get('unknown_disposition_events',0)}")
    con.close()


def cmd_report(args: argparse.Namespace) -> None:
    from .db import get_connection
    from .ingest_leads import load_dispositions
    from .reports import generate_reports

    db_path = Path(args.db).resolve()
    out_dir = Path(args.out).resolve()
    schemas_dir = Path(args.schemas).resolve()

    con = get_connection(db_path)
    dispositions = load_dispositions(schemas_dir)
    generate_reports(con, out_dir, dispositions)
    con.close()


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    common.add_argument("--schemas", default=str(DEFAULT_SCHEMAS), help="Schemas directory")

    parser = argparse.ArgumentParser(
        prog="python -m solution",
        description="Interaction Ledger & Reporting Pipeline",
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command", required=True)

    ingest_p = sub.add_parser("ingest", parents=[common], help="Ingest a drop directory")
    ingest_p.add_argument("drop_path", help="Path to drop directory (e.g. data/drop_1)")

    report_p = sub.add_parser("report", parents=[common], help="Generate reports")
    report_p.add_argument("--out", required=True, help="Output directory for report files")

    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
