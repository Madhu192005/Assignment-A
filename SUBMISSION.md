# Submission

## Candidate
- **Name:** Madhusudhanan
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~7h

## How to run
```bash
# 1. Setup virtual environment and dependencies
python -m venv .venv
# Linux / macOS:
# source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. (Optional) Generate synthetic data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 3. Ingest drops into the append-only ledger
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2

# 4. Generate the five report files
python -m solution report --out reports/

# 5. Run automated test suite
pytest solution/tests/ -v
```

## Design

### Architecture Overview
The pipeline is structured as an idempotent, incremental data processing system built on top of SQLite with WAL (Write-Ahead Logging) mode.

```
       [Leads CSV Drop]                      [Events JSONL Drop]
               │                                      │
               ▼                                      ▼
     Header Normalisation                  JSON Validation & Dedup
   & Schema-order Validation                          │
               │                                      ▼
               ▼                             Timestamp Normalisation
     PII Encryption & Hash                      (UTC, Offset, Naive IST)
               │                                      │
               ▼                                      ▼
      Merge / Deduplication                 Lead Linking by Phone Hash
   (First batch permanent stem)             & Override Call-ID resolution
               │                                      │
               ▼                                      ▼
       [leads Table]                         [events Ledger]
               │                                      │
               └──────────────────┬───────────────────┘
                                  │
                                  ▼
                         Reporting Aggregator
                 (O(N) single-pass memory projection)
                                  │
          ┌───────────────┬───────┴───────┬───────────────┬──────────────┐
          ▼               ▼               ▼               ▼              ▼
  batch_summary.csv daily_overview.csv lead_report.csv rejections.csv data_quality.json
```

### Database Schema & Modeling

The database uses five tables designed to separate immutable ledger logs from identity state and data quality auditing:

1. **`leads`**: Identity dimension table.
   - **Identity**: `(workflow_id, phone_hash)` with a `UNIQUE` constraint.
   - **Batch attribution**: `batch_id` permanently retains the file stem of the first batch that created the lead.
   - **PII at rest**: `phone_enc` and `customer_name_enc` store AES-128-CBC encrypted data. Plaintext phone numbers and customer names are never stored in the database. `phone_masked` (`+91******XXXX`) is precomputed for reporting.
   - **Merging**: Subsequent valid rows for the same identity merge non-null fields (last-write-wins) without altering `batch_id`.

2. **`events`**: Append-only interaction ledger.
   - **Immutability**: Every event has a unique `event_id`. Overrides never delete or edit prior events; they append as `disposition.override`.
   - **Linking**: Links to `leads.id` via `lead_id` foreign key. Unlinked events are ingested with `lead_id = NULL` and retroactively linked when subsequent lead batches arrive.
   - **Indexes**: Indexed on `call_id`, `lead_id`, `phone_hash`, and `event_type` to allow rapid joins and chronological queries.

3. **`rejections`**: Audit log of invalid lead rows.
   - Stores `(workflow_id, batch_id, row_number, reason)` with zero PII.
   - Exactly reflects the first failure encountered when walking fields in schema order.

4. **`batches` & `drops`**: Idempotency registers.
   - `batches`: Tracks ingested `(drop_path, workflow_id, batch_id)` tuples.
   - `drops`: Tracks completed drop ingestions so that re-running `ingest` on the same directory is a fast no-op.

5. **`_dq_stats`**: High-water mark counters for `data_quality.json`, ensuring non-event metrics (total rows read, dropped duplicates, malformed lines, field warnings) accumulate incrementally across drops.

### PII Protection at Rest
- To satisfy the requirement that PII fields (`phone`, `customer_name`) are never stored in plaintext while still allowing events to link to leads by phone number, we use a two-tier cryptographic approach:
  - **Deterministic join token (`phone_hash`)**: Canonical phone numbers (`+91XXXXXXXXXX`) are hashed with SHA-256. This enables exact matching of events to leads without decrypting data.
  - **Encrypted payload (`phone_enc`, `customer_name_enc`)**: AES-128-CBC encryption with a fresh random 16-byte IV per record. The IV is prepended to the ciphertext (`iv:ciphertext`), preventing pattern matching across identical names. A configurable key is derived from the `LEDGER_SECRET` environment variable.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| **SQLite with WAL mode for database** | Zero external dependencies, fast setup, robust transactions, and satisfies "Store data in a real database". | PostgreSQL (requires running container; adds network overhead for take-home evaluation). |
| **Store deterministic SHA-256 phone hash** | Allows matching events to leads via phone number without decrypting phone numbers or storing plaintext PII. | Decrypting all phone numbers on each event link (orders of magnitude slower, exposes PII in memory). |
| **Apply schema defaults only to fields remaining null** | The spec states: *"Defaults apply after merging, to fields that are still null."* Applying defaults before merging would cause a default value in a duplicate row to overwrite an earlier valid non-null value. | Applying defaults on raw CSV parsing (violates spec duplicate merge rules). |
| **Unknown dispositions skipped from ledger** | The spec states: *"Unknown disposition: a disposition.* event whose code is not in dispositions.csv... count it and ignore it."* | Storing unknown events in the ledger (would pollute report aggregations and lead bucket statuses). |
| **Late-arriving orphan resolution** | Events can arrive in `drop_1` before leads in `drop_2`. Upon lead ingest, orphaned events and unlinked overrides are retroactively linked. | Discarding orphaned events immediately (would produce inaccurate reports when leads arrive later). |
| **In-memory single-pass projection for reporting** | Aggregating call state and effective dispositions in memory in O(N) takes under 0.5s instead of running thousands of separate queries (N+1 query problem). | Running subqueries per lead in SQL (takes minutes on large scale). |

## Failure modes considered

- **Dirty input / missing fields**: Handled by schema-order field validation. Missing required fields produce `missing_required:<field>`. Invalid phone formats produce `invalid_phone`. Blank rows (all empty/whitespace) are ignored without affecting row counts.
- **Malformed event lines**: Any line that is invalid JSON or lacks `event_id` is counted under `malformed_events` and skipped without aborting the ingest process.
- **Duplicate events**: Duplicate `event_id` occurrences (within a file or across drops) are caught by checking existing event IDs, counted under `duplicate_events_dropped`, and skipped.
- **Idempotency on re-runs**: Ingesting the same drop path multiple times detects existing ingestion markers and exits cleanly without duplicating counts, leads, or ledger events.
- **Out-of-order events**: Event timestamps are parsed into standardized UTC ISO strings. Late overrides correctly supersede earlier disposition sets by timestamp ordering regardless of arrival sequence.
- **What is NOT handled**: Real-time concurrent ingestion across multiple distributed workers (distributed locks / Kafka streaming required for production scale).

## Testing

Automated test suite (`solution/tests/test_pipeline.py`) contains 63 tests covering:
- **Phone normalisation**: 10-digit bare, `+91`, `91-` with dashes, `0` prefix, parenthesized, landline rejections, bad prefixes (5x), 9-digit numbers, non-Indian numbers.
- **Date formats**: `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY` (case-insensitive), impossible dates (31/02), text strings.
- **Decimal parsing**: Currency symbols (`₹`, `Rs`, `Rs.`, `INR`), commas as thousands separators, rounding to 2 decimal places, negative rejections, non-numeric strings.
- **Enum normalisation**: Exact, uppercase, mixed case, invalid values.
- **Header normalisation & aliasing**: Whitespace collapsing, underscores, alias matching.
- **PII encryption**: Round-trip encryption/decryption, unique IV generation per record.
- **Bucket logic**: `yet_to_reach_out`, `completed` (terminal disposition), `in_progress_telephony_failed` (all calls failed), `in_progress_dialed`.
- **Effective disposition**: Prioritizing `disposition.override` over `disposition.set`, resolution of latest override by timestamp.
- **Timestamp parsing**: UTC `Z`, timezone offsets `+05:30`, naive timestamps parsed as IST.

All 63 tests pass via `pytest solution/tests/ -v`.

## Results
- **Timings (Small Scale: ~2,000 leads, ~8,700 events)**:
  - Ingest `drop_1`: ~24s
  - Ingest `drop_2`: ~28s
  - Reports generation: ~0.4s
  - Total end-to-end execution: ~1m 04s
  - Automated tests (63 tests): 1.1s
- **Observations in Data**:
  - ~3% duplicate leads merged across batches (e.g. 57 duplicates merged).
  - 1,123 events were initially orphaned in `drop_1`, but dropped to 461 after `drop_2` arrived as late-arriving leads were matched.
  - All five report files generated from incremental drops (`drop_1` then `drop_2`) match 100% byte-for-byte against ingesting all drops at once.

## If I had another day
1. **Bulk SQL Batched Ingestion**: Replace row-by-row sqlite inserts with `executemany` or SQLite native CSV import for 10x higher ingestion throughput on `--scale large`.
2. **DuckDB / Postgres In-Engine Window Functions**: Move the effective disposition and lead bucket calculation directly into SQL using `ROW_NUMBER() OVER (PARTITION BY call_id ORDER BY occurred_at DESC)` for zero-copy reporting.
3. **Async / Parallel File Parsing**: Process multiple lead CSVs and JSONL event chunks concurrently using `concurrent.futures`.
4. **Data Contract Schema Registry**: Support dynamic JSON Schema / Pydantic models for arbitrary workflow schemas beyond the initial two.
