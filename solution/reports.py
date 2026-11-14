"""
Report generation: produces all five output files from the DB.

1. batch_summary.csv
2. daily_overview.csv
3. lead_report.csv
4. rejections.csv
5. data_quality.json
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def _utc_to_ist(utc_iso: str) -> datetime:
    # Handles timestamps ending in 'Z' or '+HH:MM'
    clean = utc_iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(clean)
    return dt.astimezone(IST)


def _ist_date(utc_iso: str) -> str:
    return _utc_to_ist(utc_iso).strftime("%Y-%m-%d")


def _ist_iso(utc_iso: str) -> str:
    return _utc_to_ist(utc_iso).strftime("%Y-%m-%dT%H:%M:%S+05:30")


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Precomputed Call and Lead Aggregator (High-Performance O(N))
# ---------------------------------------------------------------------------

class CallData:
    __slots__ = (
        "call_id",
        "lead_id",
        "workflow_id",
        "initiated_at_utc",
        "has_connected",
        "has_telephony_failed",
        "latest_override_code",
        "latest_override_time",
        "latest_set_code",
        "latest_set_time",
    )

    def __init__(self, call_id: str, lead_id: int, workflow_id: str):
        self.call_id = call_id
        self.lead_id = lead_id
        self.workflow_id = workflow_id
        self.initiated_at_utc: str | None = None
        self.has_connected = False
        self.has_telephony_failed = False
        self.latest_override_code: str | None = None
        self.latest_override_time: str | None = None
        self.latest_set_code: str | None = None
        self.latest_set_time: str | None = None

    def effective_disposition(self, dispositions: dict) -> str | None:
        """Latest override if known disposition, else latest set if known disposition."""
        wf_disps = dispositions[self.workflow_id] if self.workflow_id in dispositions else dispositions
        if self.latest_override_code and self.latest_override_code in wf_disps:
            return self.latest_override_code
        if self.latest_set_code and self.latest_set_code in wf_disps:
            return self.latest_set_code
        return None


def _load_calls_map(con: sqlite3.Connection) -> dict[str, CallData]:
    """
    Load and aggregate all linked events into CallData objects.
    Returns: {call_id: CallData}
    """
    rows = con.execute(
        """
        SELECT call_id, lead_id, workflow_id, event_type, occurred_at_utc, payload
        FROM events
        WHERE lead_id IS NOT NULL
        ORDER BY occurred_at_utc ASC
        """
    ).fetchall()

    calls: dict[str, CallData] = {}
    for r in rows:
        cid = r["call_id"]
        if cid not in calls:
            calls[cid] = CallData(cid, r["lead_id"], r["workflow_id"])
        c = calls[cid]

        etype = r["event_type"]
        ts = r["occurred_at_utc"]

        if etype == "call.initiated":
            # Per spec, call is counted if call.initiated is present
            c.initiated_at_utc = ts
        elif etype == "call.connected":
            c.has_connected = True
        elif etype == "call.telephony_failed":
            c.has_telephony_failed = True
        elif etype == "disposition.override":
            p = json.loads(r["payload"])
            code = p.get("disposition_code")
            if code:
                # Latest override by occurred_at
                if c.latest_override_time is None or ts >= c.latest_override_time:
                    c.latest_override_code = code
                    c.latest_override_time = ts
        elif etype == "disposition.set":
            p = json.loads(r["payload"])
            code = p.get("disposition_code")
            if code:
                # Latest set by occurred_at
                if c.latest_set_time is None or ts >= c.latest_set_time:
                    c.latest_set_code = code
                    c.latest_set_time = ts

    return calls


def _compute_lead_bucket(calls: list[CallData], dispositions: dict) -> str:
    """
    1. yet_to_reach_out: no calls.
    2. completed: any call's effective disposition is terminal (is_terminal=true).
    3. in_progress_telephony_failed: every call has a call.telephony_failed event.
    4. in_progress_dialed: everything else.
    """
    if not calls:
        return "yet_to_reach_out"

    # Terminal?
    for c in calls:
        eff = c.effective_disposition(dispositions)
        if eff:
            wf_disps = dispositions[c.workflow_id] if c.workflow_id in dispositions else dispositions
            if wf_disps.get(eff, {}).get("is_terminal"):
                return "completed"

    # All telephony_failed?
    if all(c.has_telephony_failed for c in calls):
        return "in_progress_telephony_failed"

    return "in_progress_dialed"


# ---------------------------------------------------------------------------
# Report 1: batch_summary.csv
# ---------------------------------------------------------------------------

def report_batch_summary(
    con: sqlite3.Connection,
    out_dir: Path,
    dispositions: dict,
    lead_calls: dict[int, list[CallData]],
) -> None:
    batches_q = con.execute(
        "SELECT DISTINCT workflow_id, batch_id FROM leads ORDER BY workflow_id, batch_id"
    ).fetchall()

    rows = []
    for b in batches_q:
        workflow_id = b["workflow_id"]
        batch_id = b["batch_id"]

        leads = con.execute(
            "SELECT id FROM leads WHERE workflow_id=? AND batch_id=?",
            (workflow_id, batch_id),
        ).fetchall()

        buckets = defaultdict(int)
        for lrow in leads:
            lid = lrow["id"]
            calls = lead_calls.get(lid, [])
            bucket = _compute_lead_bucket(calls, dispositions)
            buckets[bucket] += 1

        total = len(leads)
        rows.append({
            "workflow_id": workflow_id,
            "batch_id": batch_id,
            "total_leads": total,
            "yet_to_reach_out": buckets["yet_to_reach_out"],
            "in_progress_telephony_failed": buckets["in_progress_telephony_failed"],
            "in_progress_dialed": buckets["in_progress_dialed"],
            "completed": buckets["completed"],
        })

    rows.sort(key=lambda r: (r["workflow_id"], r["batch_id"]))
    write_csv(
        out_dir / "batch_summary.csv",
        rows,
        [
            "workflow_id",
            "batch_id",
            "total_leads",
            "yet_to_reach_out",
            "in_progress_telephony_failed",
            "in_progress_dialed",
            "completed",
        ],
    )


# ---------------------------------------------------------------------------
# Report 2: daily_overview.csv
# ---------------------------------------------------------------------------

def report_daily_overview(
    out_dir: Path,
    dispositions: dict,
    all_calls: list[CallData],
) -> None:
    # A call is counted only if call.initiated is present
    daily: dict[tuple[str, str], dict] = {}

    for c in all_calls:
        if not c.initiated_at_utc:
            continue
        ist_date_str = _ist_date(c.initiated_at_utc)
        key = (c.workflow_id, ist_date_str)
        if key not in daily:
            daily[key] = {
                "total_calls": 0,
                "connected_calls": 0,
                "success_leads": set(),
            }

        daily[key]["total_calls"] += 1
        if c.has_connected:
            daily[key]["connected_calls"] += 1

        eff = c.effective_disposition(dispositions)
        if eff:
            wf_disps = dispositions.get(c.workflow_id, {})
            if wf_disps.get(eff, {}).get("is_success"):
                daily[key]["success_leads"].add(c.lead_id)

    rows = []
    for (wf, dt_str), vals in sorted(daily.items()):
        tot = vals["total_calls"]
        conn = vals["connected_calls"]
        rate = f"{(conn / tot):.4f}" if tot > 0 else "0.0000"
        rows.append({
            "workflow_id": wf,
            "date": dt_str,
            "total_calls": tot,
            "connected_calls": conn,
            "connectivity_rate": rate,
            "success_leads": len(vals["success_leads"]),
        })

    rows.sort(key=lambda r: (r["workflow_id"], r["date"]))
    write_csv(
        out_dir / "daily_overview.csv",
        rows,
        [
            "workflow_id",
            "date",
            "total_calls",
            "connected_calls",
            "connectivity_rate",
            "success_leads",
        ],
    )


# ---------------------------------------------------------------------------
# Report 3: lead_report.csv
# ---------------------------------------------------------------------------

def report_lead_report(
    con: sqlite3.Connection,
    out_dir: Path,
    dispositions: dict,
    lead_calls: dict[int, list[CallData]],
) -> None:
    leads = con.execute(
        "SELECT id, workflow_id, external_ref, batch_id, phone_masked FROM leads ORDER BY workflow_id, external_ref"
    ).fetchall()

    rows = []
    for lead in leads:
        lid = lead["id"]
        wf = lead["workflow_id"]
        calls = lead_calls.get(lid, [])

        bucket = _compute_lead_bucket(calls, dispositions)
        total_calls = len(calls)
        connected_calls = sum(1 for c in calls if c.has_connected)

        last_call_at = ""
        current_disposition = ""
        ever_success = False

        if calls:
            # Sort by initiated time
            sorted_calls = sorted(
                calls, key=lambda c: c.initiated_at_utc or ""
            )
            latest_call = sorted_calls[-1]
            if latest_call.initiated_at_utc:
                last_call_at = _ist_iso(latest_call.initiated_at_utc)

            # current_disposition: effective disposition of most recent call (by initiated time) that has one
            for c in reversed(sorted_calls):
                eff = c.effective_disposition(dispositions)
                if eff:
                    current_disposition = eff
                    break

            # ever_success
            for c in sorted_calls:
                eff = c.effective_disposition(dispositions)
                if eff:
                    wf_disps = dispositions.get(wf, {})
                    if wf_disps.get(eff, {}).get("is_success"):
                        ever_success = True
                        break

        rows.append({
            "workflow_id": wf,
            "external_ref": lead["external_ref"] or "",
            "batch_id": lead["batch_id"],
            "phone_masked": lead["phone_masked"],
            "bucket": bucket,
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "last_call_at": last_call_at,
            "current_disposition": current_disposition,
            "ever_success": str(ever_success).lower(),
        })

    rows.sort(key=lambda r: (r["workflow_id"], r["external_ref"]))
    write_csv(
        out_dir / "lead_report.csv",
        rows,
        [
            "workflow_id",
            "external_ref",
            "batch_id",
            "phone_masked",
            "bucket",
            "total_calls",
            "connected_calls",
            "last_call_at",
            "current_disposition",
            "ever_success",
        ],
    )


# ---------------------------------------------------------------------------
# Report 4: rejections.csv
# ---------------------------------------------------------------------------

def report_rejections(con: sqlite3.Connection, out_dir: Path) -> None:
    rows_q = con.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM rejections
        ORDER BY workflow_id, batch_id, row_number
        """
    ).fetchall()

    rows = [dict(r) for r in rows_q]
    write_csv(
        out_dir / "rejections.csv",
        rows,
        ["workflow_id", "batch_id", "row_number", "reason"],
    )


# ---------------------------------------------------------------------------
# Report 5: data_quality.json
# ---------------------------------------------------------------------------

def report_data_quality(con: sqlite3.Connection, out_dir: Path) -> None:
    try:
        row = con.execute("SELECT * FROM _dq_stats WHERE id=1").fetchone()
    except Exception:
        row = None

    # Orphan count reflects final state
    orphans_live = con.execute(
        "SELECT COUNT(*) FROM events WHERE lead_id IS NULL"
    ).fetchone()[0]

    if row:
        rbt = json.loads(row["rejections_by_reason"])
        result = {
            "lead_rows_total": row["lead_rows_total"],
            "lead_rows_rejected": row["lead_rows_rejected"],
            "rejections_by_reason": rbt,
            "duplicate_lead_rows_merged": row["duplicate_lead_rows_merged"],
            "field_warnings": row["field_warnings"],
            "events_total": row["events_total"],
            "malformed_events": row["malformed_events"],
            "duplicate_events_dropped": row["duplicate_events_dropped"],
            "orphan_events": orphans_live,
            "unknown_disposition_events": row["unknown_disposition_events"],
        }
    else:
        lead_rows_total = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        lead_rows_rejected = con.execute("SELECT COUNT(*) FROM rejections").fetchone()[0]
        rej_rows = con.execute(
            "SELECT reason, COUNT(*) as cnt FROM rejections GROUP BY reason"
        ).fetchall()
        rbt = {r["reason"]: r["cnt"] for r in rej_rows}
        result = {
            "lead_rows_total": lead_rows_total + lead_rows_rejected,
            "lead_rows_rejected": lead_rows_rejected,
            "rejections_by_reason": rbt,
            "duplicate_lead_rows_merged": 0,
            "field_warnings": 0,
            "events_total": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "malformed_events": 0,
            "duplicate_events_dropped": 0,
            "orphan_events": orphans_live,
            "unknown_disposition_events": 0,
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data_quality.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Compatibility Helpers for Unit Tests
# ---------------------------------------------------------------------------

def _get_effective_disposition(con: sqlite3.Connection, call_id: str, lead_id: int, dispositions: dict) -> str | None:
    calls_map = _load_calls_map(con)
    c = calls_map.get(call_id)
    return c.effective_disposition(dispositions) if c else None


def _get_calls_for_lead(con: sqlite3.Connection, lead_id: int) -> list[dict]:
    rows = con.execute(
        """
        SELECT call_id, occurred_at_utc FROM events
        WHERE lead_id=? AND event_type='call.initiated'
        ORDER BY occurred_at_utc
        """,
        (lead_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _compute_bucket(
    calls: list[dict],
    con: sqlite3.Connection,
    lead_id: int,
    wf_dispositions: dict,
) -> str:
    calls_map = _load_calls_map(con)
    lead_c = [
        calls_map[c["call_id"]]
        for c in calls
        if c["call_id"] in calls_map and calls_map[c["call_id"]].lead_id == lead_id
    ]
    return _compute_lead_bucket(lead_c, wf_dispositions)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_reports(con: sqlite3.Connection, out_dir: Path, dispositions: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Preload and group calls
    calls_map = _load_calls_map(con)
    # A call is counted only if its call.initiated event is present (and linked)
    initiated_calls = [c for c in calls_map.values() if c.initiated_at_utc is not None]

    lead_calls: dict[int, list[CallData]] = defaultdict(list)
    for c in initiated_calls:
        lead_calls[c.lead_id].append(c)

    # 2. Generate each report
    report_batch_summary(con, out_dir, dispositions, lead_calls)
    report_daily_overview(out_dir, dispositions, initiated_calls)
    report_lead_report(con, out_dir, dispositions, lead_calls)
    report_rejections(con, out_dir)
    report_data_quality(con, out_dir)
    print(f"Reports successfully written to {out_dir}/")
