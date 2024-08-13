# Agents Guide

This file provides guidance for AI agents working in this repository.

## Project Overview

Transparency Archiver is a Python-based document capture pipeline. It reads company/URL data from MongoDB, fetches current document content via a custom BrowsertrixCrawler fork, compares snapshots, and stores new captures in GCP Storage. The main entry point is `main.py`.

## Environment Setup

Full setup after cloning:
```
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
```
python main.py --dry_run true
```
Dry run skips all writes to MongoDB and GCP Storage, and preserves temporary files for inspection.

To lint:
```
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
- `source/async_compare.py` — fuzzy comparison logic for all 5 formats; contains `has_content_changed`, `compare_files`, `validate_file`
- `source/changes.py` — generates `.CHANGES.json` diff objects using the patience diff algorithm
- `source/utils.py` — shared utilities: MongoDB connection, GCS helpers, `DocumentCache`, `format_data`
- `source/globals.py` — `CFG` config object and shared `logger`; initialized once at startup
- `config.yaml` — runtime configuration: priority tiers, fuzzy threshold, topic patterns, format settings
- `docker-compose.yml` / `Dockerfile` — container setup for production-equivalent runs
- `requirements.txt` — runtime dependencies
- `requirements-dev.txt` — dev dependencies (`pre-commit`, `ruff`)

## Key Behaviors to Preserve

- **Change detection**: A new snapshot is only saved if content has changed from the most recent capture. This is determined by fuzzy ratio (`rapidfuzz`) against a configurable threshold in `config.yaml`. Do not bypass or weaken this check.
- **Format handling**: Each document is stored in up to 5 formats (`txt`, `json`, `html`, `pdf`, `wacz`). Each format is compared and stored independently — a change in one format does not imply a change in another.
- **`warc` format is always treated as changed**: `_compare_files_sync` in `source/async_compare.py` returns `True` unconditionally for `format == "warc"`. This is intentional — WARC files are too large and complex to diff reliably. Do not add comparison logic for this format.
- **Fail counter**: `fail_counter` increments on all rejected documents, not just errored ones — including cases where all formats are rejected as unchanged. Do not change this to only count errors.
- **Notifications**: Slack notifications must only fire when a document actually changed (i.e., a new snapshot was saved), not on every successful pipeline run.

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
