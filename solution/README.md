# Interaction Ledger & Reporting Pipeline (Assignment A Solution)

Complete implementation of the data pipeline for ingesting messy lead CSVs and call events, storing an append-only ledger in SQLite, encrypting PII at rest, and generating accurate morning reports.

## Structure

```
solution/
├── __init__.py
├── __main__.py          # CLI entry point: `python -m solution ingest` / `report`
├── crypto.py            # AES-128-CBC encryption & SHA-256 deterministic tokenisation
├── db.py                # Database schema, connections, and indexes (SQLite + WAL)
├── normalise.py         # Type parsing & validation: phone, date, decimal, enum, headers
├── ingest_leads.py      # Lead ingestion: schema matching, validation, rejection, merging
├── ingest_events.py     # Event ingestion: dedupe, phone-hash linking, late orphan resolution
├── reports.py           # Report generation: O(N) single-pass projection of the 5 report files
└── tests/
    ├── __init__.py
    └── test_pipeline.py # 63 comprehensive unit tests
```

## Running the Pipeline

```bash
# Ingest drop 1
python -m solution ingest data/drop_1

# Ingest drop 2 (incremental)
python -m solution ingest data/drop_2

# Produce all 5 reports in reports/
python -m solution report --out reports/

# Run test suite
pytest solution/tests/ -v
```
