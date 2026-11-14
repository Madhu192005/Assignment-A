# AI Usage Disclosure

## Tools used
Claude (Sonnet 4.6), Gemini (Flash)

## What I used them for
- Scaffolding the repository layout, initial CLI argument structure, and boilerplate SQLite schema definitions.
- Brainstorming regex patterns for multi-format Indian date strings (`DD-Mon-YYYY`, `DD/MM/YYYY`, `YYYY-MM-DD`) and phone number sanitization.
- Drafting initial unit tests for phone sanitization, decimal parsing, and bucket state machine logic.

## What I wrote or decided myself
- **Core data architecture and ledger schema**: The dual-layer PII approach using deterministic SHA-256 phone hashes for indexing/joining alongside AES-128-CBC encryption with unique IVs for sensitive fields.
- **Reporting aggregation optimization**: Identifying that querying SQLite in an N+1 loop per lead was causing severe latency, and redesigning the report generation into an O(N) single-pass in-memory projection with `CallData` objects.
- **Spec nuances and duplicate merge timing**: Ensuring that defaults are applied *only* after merging duplicate rows to prevent default values from overwriting existing non-null values.
- **Late-arriving orphan resolution**: Implementing the two-stage linking mechanism that revisits unlinked events and supervisor overrides when subsequent lead drops arrive.
- **Subcommand CLI argument parsing**: Designing parent argument parsers so that `--db` and `--schemas` work reliably before or after subcommands.

## Where the AI was wrong or unhelpful
- **Initial Report Performance (N+1 Query Bottleneck)**: The initial AI suggestion for report generation executed individual SQL queries for each call and each lead to determine buckets and effective dispositions. When tested on the dataset, this caused report generation to hang. I intervened, killed the task, and refactored the reporting logic to load events into memory once and aggregate in O(N) time, dropping the report time to under 0.5 seconds.
- **Default Value Precedence**: Early draft logic applied default values (e.g. `preferred_language = "en"`) before deduplication merging. This caused a duplicate row with missing language to overwrite a previously valid `ta` language with the default `en`. I noticed this violation of the spec and ensured defaults only apply to fields that remain null after all merging.
- **Decimal Normalisation Test Expectations**: The initial test draft asserted that `"12345"` should normalize to `"12345"`, whereas the spec specifies rounding to 2 decimal places (`"12345.00"`). I updated the tests to strictly enforce two decimal places.

## How I verified AI-generated code
- **63 Unit Tests**: Ran a comprehensive pytest test suite covering phone normalisation, date parsing, currency formatting, enum values, crypto round-tripping, and bucket precedence rules.
- **End-to-End Ingest & Report Runs**: Generated synthetic drops using the official `generate_data.py` tool, executed full ingest runs across `drop_1` and `drop_2`, and verified all five generated report files against the output contract.
- **Differential Verification**: Ran `Compare-Object` across report outputs generated from incremental drops (`drop_1` then `drop_2`) versus a combined run to confirm 100% byte-for-byte reproducibility and idempotency.
