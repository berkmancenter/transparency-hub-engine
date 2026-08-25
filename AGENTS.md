# Agents Guide

This file provides guidance for AI agents working in this repository.

## Project Overview

Transparency Archiver is a Python-based document capture pipeline. It reads company/URL data from MongoDB, fetches current document content via a custom BrowsertrixCrawler fork, compares snapshots, and stores new captures in GCP Storage. The main entry point is `main.py`.

## Environment Setup

Full setup after cloning:

```bash
# 1. Install Python 3.13.3 (required)
# 2. Install dev dependencies and activate pre-commit hooks
pip install -r requirements.txt
pip install -r requirements-dev.txt
pre-commit install

# 3. Set required environment variables
export MONGODB_URL='mongodb://localhost:27017/'
export GCP_BUCKET='document_store'
```

Secrets are stored in **GCP Secret Manager** in production (e.g., `MONGODB_URL` is injected as a secret via Cloud Run). Locally, use a `.env` file (gitignored) or export variables in your shell. Never hardcode credentials.

Docker is required for BrowsertrixCrawler captures. Ensure the custom fork image is built and available in your local Docker engine before running the pipeline.

## Build and Test Commands

There is no automated test suite. Use `--dry_run` to verify logic without writing to external services:

```bash
python main.py --dry_run true
```

Dry run skips all writes to MongoDB and GCP Storage, and preserves temporary files for inspection.

To lint:

```bash
ruff format && ruff check --fix
```

The pre-commit hook runs this automatically on every commit.

## Code Style and Conventions

- **Python 3.13.3**
- **Formatter/linter**: `ruff` (see `.pre-commit-config.yaml` for pinned version)
- **Indentation**: 4 spaces (enforced by ruff)
- **Quotes**: double quotes (enforced by ruff)
- **Naming**: `camelCase` for local variables, `snake_case` for module-level functions
- Keep capture, comparison, and storage logic separated in `source/` — do not let pipeline orchestration bleed into storage or comparison modules

Good pattern — thin pipeline, separate concerns:

```python
# source/pipeline.py
result = capture_document(url)
if validate_file(result.path, result.format):
    if compare_files(result.path, gcp_path, result.format, bucket):
        store_document(result)
```

Bad pattern — mixing concerns:

```python
# Don't do this: orchestration logic inside a storage function
def store_document(result):
    if fuzz.ratio(old, new) > threshold:  # comparison doesn't belong here
        upload_to_gcs(result)
```

## Project Structure

- `main.py` — orchestrates a full DB sync run; entry point for Docker and local runs
- `source/pipeline.py` — `DocumentPipeline` class; drives per-document processing
- `source/process.py` — `ProcessCrawl` class; turns a completed crawl into Document-collection records, including `reconstruct_missing_txt` (backfills a `.txt` from another captured format when the crawl didn't produce one directly)
- `source/async_compare.py` — fuzzy comparison logic for all 5 formats; contains `has_content_changed`, `compare_files`, `validate_file`, `passes_policy_document_check`, `reconstruct_text_from_format`
- `source/policy_classifier.py` — inference wrapper around the trained policy-document classifier (model trained offline via `scripts/build_policy_dataset.py` and `scripts/train_policy_classifier.py`, loaded from `models/policy_classifier.joblib`)
- `source/changes.py` — generates `.CHANGES.json` diff objects using the patience diff algorithm
- `source/utils.py` — shared utilities: MongoDB connection, GCS helpers, `DocumentCache`, `format_data`
- `source/globals.py` — `CFG` config object and shared `logger`; initialized once at startup
- `config.yaml` — runtime configuration: priority tiers, fuzzy threshold, topic patterns, format settings, policy-classifier toggle/threshold, boundary-classifier toggle/threshold
- `docker-compose.yml` / `Dockerfile` — container setup for production-equivalent runs
- `requirements.txt` — runtime dependencies
- `requirements-dev.txt` — dev dependencies (`pre-commit`, `ruff`)

## Key Behaviors to Preserve

- **Change detection**: A new snapshot is only saved if content has changed from the most recent capture. This is determined by fuzzy ratio (`rapidfuzz`) against a configurable threshold in `config.yaml`. Do not bypass or weaken this check.
- **Format handling**: Each document is stored in up to 5 formats (`txt`, `json`, `html`, `pdf`, `wacz`). Each format is compared and stored independently — a change in one format does not imply a change in another. All formats captured for the same company_id + type + date_fetched live on a single Mongo document, under a nested `formats` dict keyed by format name (e.g. `formats.txt.extension`) — see README.md's "document" schema example. `path`/`public_url` on that document are extension-less; the real file extension for a format (which is not always the format name — `warc` has used both `.warc.gz` and `.wacz` historically) lives at `formats.<name>.extension`.
- **`warc` format is always treated as changed**: `_compare_files_sync` in `source/async_compare.py` returns `True` unconditionally for `format == "warc"`. This is intentional — WARC files are too large and complex to diff reliably. Do not add comparison logic for this format.
- **Fail counter**: `fail_counter` increments on all rejected documents, not just errored ones — including cases where all formats are rejected as unchanged. Do not change this to only count errors.
- **Notifications**: Slack notifications must only fire when a document actually changed (i.e., a new snapshot was saved), not on every successful pipeline run.
- **Policy-document classifier**: `source/async_compare.py`'s `validate_file`/`_validate_file_sync` runs the trained classifier (`source/policy_classifier.py`) against every `txt` format capture -- live or reconstructed -- and rejects it if the model is confident it's not a policy document (homepage, cookie wall, 404, login wall, ...). Controlled by `USE_POLICY_CLASSIFIER`/`POLICY_CLASSIFIER_THRESHOLD` in `config.yaml`. It only runs for `txt`, never for `html`/`pdf`/`warc.json`/`warc` validation, since the model was trained on the live pipeline's own `.txt` text. It fails open (allows the capture) if the model can't be loaded or run -- do not make this fail closed, or a missing/corrupt model file would silently block every capture.
- **Missing `.txt` reconstruction**: `ProcessCrawl.reconstruct_missing_txt` (`source/process.py`) runs once per crawl, only when no `txt` format was captured directly. It tries `RECONSTRUCTION_FORMAT_PRIORITY` (`html > pdf > warc.json > warc`) in order, extracting text via `reconstruct_text_from_format` and running it through the exact same `verify_content` path (quality check, classifier, fuzzy-diff against the previous `.txt`) a directly captured `.txt` goes through, stopping at the first format that produces a validated result. This mirrors `scripts/reconstruct_missing_txt.py`'s offline backfill logic -- keep the two in sync if you change either.
- **Boundary (header/footer) diff trimmer**: `source/changes.py`'s `get_diff_text`/`_fetch_html_trimmed` can trim header/footer/nav boilerplate off the text used to build a `.CHANGES.json` diff, via `source/boundary_classifier.py`. This is cosmetic only -- it shapes what the Comparison Tool displays, nothing else. It never touches `source/async_compare.py`'s change-detection path (`has_content_changed`, `compare_html_files`, `verify_content`, ...), which always runs on the full untrimmed text regardless of this setting -- do not wire the trimmer into that path, since it would let a boundary-classifier mistake affect whether a capture is archived, not just how a diff is displayed. Controlled by `USE_DIFF_TRIMMER`/`DIFF_TRIMMER_THRESHOLD` in `config.yaml` (defaults to off); fails open to the untrimmed `.txt` if the model can't be loaded/run, or if either side of the pair lacks an `html` format (both sides always use the same text source -- trimming only one side would diff trimmed text against untrimmed text and show extraction-style noise as if it were a real content change).

## Safety and Permission Boundaries

**Do autonomously:**

- Edit any file in `source/`
- Edit `config.yaml` for threshold or pattern tuning
- Run `python main.py --dry_run true`
- Run `ruff format && ruff check --fix`
- Add or update entries in `requirements-dev.txt`

**Ask before doing:**

- Running `python main.py` without `--dry_run` (writes to MongoDB and GCP)
- Running `docker-compose up` (triggers live crawls via BrowsertrixCrawler)
- Adding new runtime dependencies to `requirements.txt` or `pyproject.toml`
- Any changes to MongoDB schema or GCS path structure (breaks existing document references)
- Modifying the patience diff algorithm in `source/changes.py` (affects all historical change data)

**Never do:**

- Commit credentials, `.env` files, or GCP service account keys
- Push directly to `main`
- Drop or rename MongoDB collections or GCS path prefixes

## PR and Commit Guidelines

- Keep commits scoped: one logical change per commit
- Commit message format: `<type>: <short description>` — e.g., `fix: correct fail_counter increment logic`, `feat: add json format comparison`
- Run `--dry_run` and confirm expected log output before opening a PR
- Update `AGENTS.md` if you change setup steps, key behaviors, or permission boundaries
