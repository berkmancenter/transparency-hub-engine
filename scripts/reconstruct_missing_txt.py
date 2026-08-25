# Transparency Archiver - Online Document Capture Tool
# Backfills a .txt capture for every documents_v2 snapshot that's missing
# one, using whichever other format it already has, then decides the
# snapshot's fate:
#
#   - Reconstructed text fails data_quality_check (too short / not English)
#     or the policy-document classifier (source/policy_classifier.py) is
#     confident it's NOT a policy document -> delete the entire snapshot
#     (Mongo record + every GCS file across all its formats).
#   - Reconstructed text passes both checks but is a fuzzy-match (same
#     has_content_changed logic the live pipeline already uses) to the
#     most recent earlier .txt capture for that company/type -> delete the
#     entire snapshot (it's a redundant duplicate of an existing capture).
#   - Reconstructed text passes both checks and is either the first-ever
#     capture for that company/type or shows real changes -> upload the
#     new .txt to GCS, add it to the document's "formats" in Mongo, and
#     (on a real run) regenerate .CHANGES.json data for that company via
#     the existing source.changes.create_changes_json().
#
# Source-format priority for reconstruction: html > pdf > warc.json > warc
# (wacz). html is closest to how the live pipeline's own .txt is produced;
# pdf is a clean, purpose-built document; warc.json is the crawler's own
# pre-extracted per-page text (matched to the document's original_url when
# possible, to avoid pulling in unrelated pages visited during the crawl);
# raw warc/wacz is the heaviest and messiest, so it's the last resort.
#
# CAVEAT: the policy-document classifier was trained only on text that came
# from the LIVE pipeline's own .txt capture. Text reconstructed here via
# PyPDF2 or a different HTML-to-text pass can look noisier (broken spacing,
# missing line breaks) than what the classifier saw in training, which can
# push a genuinely valid policy document's score down for reasons that have
# nothing to do with its content. Review the dry-run report before trusting
# an automated delete decision at scale.
#
# Defaults to a DRY RUN: reconstructs and classifies everything, logs every
# decision, but touches neither Mongo nor GCS. Pass --confirm to execute.
#
# Usage:
#   python scripts/reconstruct_missing_txt.py --limit 30     # small dry run
#   python scripts/reconstruct_missing_txt.py                # full dry run
#   python scripts/reconstruct_missing_txt.py --confirm       # for real

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml
from bson import ObjectId
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source.async_compare import (  # noqa: E402
    RECONSTRUCTION_FORMAT_PRIORITY as SOURCE_FORMAT_PRIORITY,
    data_quality_check,
    has_content_changed,
    reconstruct_text_from_format,
)
from source.policy_classifier import policy_document_probability  # noqa: E402
from source.utils import decode_bytes  # noqa: E402

load_dotenv(dotenv_path=".env")

DEFAULT_DB_NAME = "transparencydb_dev"
DEFAULT_GCP_BUCKET = "transparency_hub_documents"


def load_fuzzy_threshold(config_path: str) -> int:
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        return config.get("FUZZY_RATIO", 97)
    except FileNotFoundError:
        return 97


def extract_text(doc: dict, bucket) -> tuple[str, str] | None:
    """
    Tries each available format in priority order and returns the first
    one that yields non-empty text, as (text, format_used). Uses the same
    reconstruct_text_from_format() the live pipeline uses to backfill a
    missing .txt during a capture (source/process.py
    ProcessCrawl.reconstruct_missing_txt), so this offline backfill and the
    live pipeline extract text identically.
    """
    formats = doc.get("formats", {})
    for fmt in SOURCE_FORMAT_PRIORITY:
        if fmt not in formats:
            continue
        extension = formats[fmt]["extension"]
        gcs_path = f"{doc['path']}.{extension}"
        try:
            raw_bytes = bucket.blob(gcs_path).download_as_bytes(timeout=120)
        except Exception as e:
            print(f"  Failed to fetch {gcs_path}: {e}")
            continue

        try:
            text = reconstruct_text_from_format(
                raw_bytes, fmt, extension, formats[fmt].get("original_url", "")
            )
        except Exception as e:
            print(f"  Failed to extract text from {gcs_path} ({fmt}): {e}")
            continue

        if text and text.strip():
            return text, fmt

    return None


WAYBACK_CHROME_MARKERS = ("about this capture", "collected by")


def is_wayback_chrome_only(text: str) -> bool:
    """
    Detects when reconstructed text is just the Wayback Machine's own
    replay-page chrome (capture-count banner, "About this capture /
    COLLECTED BY / TIMESTAMPS") rather than the actual archived page --
    happens when a doc_urls entry points at a web.archive.org URL and the
    crawler's page-text extraction grabbed the wrapper page instead of the
    archived page rendered inside its iframe. Found affecting ~10% of
    reconstruction candidates during backfill development; the classifier
    alone isn't reliable here since the wrapper page's title often echoes
    the real doc title (e.g. "Privacy Policy - Boss Revolution"), scoring
    deceptively high despite having no real policy content underneath.
    """
    lowered = text.lower()
    return all(marker in lowered for marker in WAYBACK_CHROME_MARKERS)


def find_previous_txt(documents, doc: dict) -> dict | None:
    query = {
        "company_id": doc["company_id"],
        "type": doc["type"],
        "formats.txt": {"$exists": True},
        "date_fetched": {"$lt": doc["date_fetched"]},
    }
    return documents.find_one(query, sort=[("date_fetched", -1)])


def process_doc(
    doc: dict, bucket, documents, policy_threshold: float, fuzzy_threshold: int
) -> dict:
    row = {
        "_id": str(doc["_id"]),
        "company_id": doc.get("company_id"),
        "type": doc.get("type"),
        "path": doc.get("path"),
        "available_formats": list(doc.get("formats", {}).keys()),
    }

    extracted = extract_text(doc, bucket)
    if extracted is None:
        row["decision"] = "delete_extraction_failed"
        row["gcs_paths_to_delete"] = [
            f"{doc['path']}.{fmt_info['extension']}"
            for fmt_info in doc.get("formats", {}).values()
        ]
        return row

    text, source_format = extracted
    row["source_format"] = source_format
    row["text_snippet"] = " ".join(text.split())[:300]

    if is_wayback_chrome_only(text):
        row["decision"] = "delete_archive_wrapper_only"
        row["gcs_paths_to_delete"] = [
            f"{doc['path']}.{fmt_info['extension']}"
            for fmt_info in doc.get("formats", {}).values()
        ]
        return row

    if not data_quality_check(text):
        row["decision"] = "delete_failed_quality_check"
        row["gcs_paths_to_delete"] = [
            f"{doc['path']}.{fmt_info['extension']}"
            for fmt_info in doc.get("formats", {}).values()
        ]
        return row

    policy_probability = policy_document_probability(text)
    row["policy_probability"] = round(policy_probability, 4)
    if policy_probability < policy_threshold:
        row["decision"] = "delete_not_policy_document"
        row["gcs_paths_to_delete"] = [
            f"{doc['path']}.{fmt_info['extension']}"
            for fmt_info in doc.get("formats", {}).values()
        ]
        return row

    previous = find_previous_txt(documents, doc)
    if previous is None:
        row["decision"] = "keep_first_capture"
        row["new_txt_text"] = text
        return row

    try:
        previous_extension = previous["formats"]["txt"]["extension"]
        previous_bytes = bucket.blob(
            f"{previous['path']}.{previous_extension}"
        ).download_as_bytes(timeout=60)
        previous_text = decode_bytes(previous_bytes)
    except Exception as e:
        row["decision"] = "error_comparing_to_previous"
        row["error"] = str(e)
        return row

    if has_content_changed(previous_text, text, threshold=fuzzy_threshold):
        row["decision"] = "keep_changed"
        row["new_txt_text"] = text
        row["compared_against"] = str(previous["_id"])
    else:
        row["decision"] = "delete_no_change"
        row["compared_against"] = str(previous["_id"])
        row["gcs_paths_to_delete"] = [
            f"{doc['path']}.{fmt_info['extension']}"
            for fmt_info in doc.get("formats", {}).values()
        ]

    return row


def main():
    parser = argparse.ArgumentParser(
        description="Reconstruct .txt captures for documents_v2 snapshots "
        "missing one, quality/policy-check them, and either update or "
        "delete the snapshot. Defaults to a dry run; pass --confirm to "
        "actually execute."
    )
    parser.add_argument("--mongodb-url", default=os.getenv("MONGODB_URL"))
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--gcp-bucket", default=DEFAULT_GCP_BUCKET)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--policy-threshold",
        type=float,
        default=0.3,
        help="Treat reconstructed text as 'not a policy document' (and "
        "delete the snapshot) below this probability. Default 0.3, matching "
        "the threshold validated in scripts/find_bad_captures.py.",
    )
    parser.add_argument("--changes-dir", default="changes")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually perform uploads/updates/deletions and regenerate "
        "changes. Without this flag, only a dry-run report is produced.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--report-output", default="scripts/data/reconstruction_report.jsonl"
    )
    args = parser.parse_args()

    if not args.mongodb_url:
        parser.error("--mongodb-url is required (or set MONGODB_URL in .env)")

    dry_run = not args.confirm
    fuzzy_threshold = load_fuzzy_threshold(args.config)
    print(f"Using fuzzy-match threshold {fuzzy_threshold} (from {args.config})")

    from google.cloud.storage import Client
    from pymongo import MongoClient
    from pymongo.server_api import ServerApi

    mongo_client = MongoClient(
        args.mongodb_url,
        server_api=ServerApi("1", strict=True, deprecation_errors=True),
    )
    documents = mongo_client[args.db_name]["documents_v2"]

    storage_client = Client()
    bucket = storage_client.bucket(args.gcp_bucket)

    candidates = list(documents.find({"formats.txt": {"$exists": False}}))
    total_candidates = len(candidates)
    if args.limit is not None:
        candidates = candidates[: args.limit]
    print(f"Found {total_candidates} documents missing a .txt capture")
    if args.limit is not None:
        print(f"Limiting this run to the first {len(candidates)}")

    if dry_run:
        print("\n=== DRY RUN — nothing will be uploaded, updated, or deleted ===\n")
    else:
        print("\n=== LIVE RUN — changes are permanent ===\n")

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_doc,
                doc,
                bucket,
                documents,
                args.policy_threshold,
                fuzzy_threshold,
            ): doc
            for doc in candidates
        }
        for i, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(
                f"  [{row['decision']}] {row['company_id']}/{row['type']} -- {row['path']}"
            )
            if i % 250 == 0:
                print(f"  ...{i}/{len(candidates)} processed")

    decision_counts = Counter(row["decision"] for row in rows)
    print(f"\nProcessed {len(rows)} candidates")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")

    report_path = Path(args.report_output)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        for row in rows:
            # Full new_txt_text is large and only needed at execution time --
            # keep the report itself readable by trimming it to a snippet.
            slim_row = {k: v for k, v in row.items() if k != "new_txt_text"}
            f.write(json.dumps(slim_row) + "\n")
    print(f"\nWrote report to {report_path}")

    if dry_run:
        print(
            "\nThis was a dry run. Re-run with --confirm to actually upload "
            "new .txt captures, update/delete documents, and regenerate "
            "changes for affected companies."
        )
        mongo_client.close()
        return

    execute(rows, documents, bucket, args)
    mongo_client.close()


def execute(rows: list[dict], documents, bucket, args):
    from source.changes import create_changes_json
    from source.globals import CFG

    kept_company_ids = set()
    execution_log: list[dict] = []
    error_count = 0

    for row in rows:
        decision = row["decision"]
        try:
            if decision in ("keep_first_capture", "keep_changed"):
                new_txt_path = f"{row['path']}.txt"
                bucket.blob(new_txt_path).upload_from_string(
                    row["new_txt_text"], content_type="text/plain"
                )
                original_url = documents.find_one({"_id": ObjectId(row["_id"])})[
                    "formats"
                ][row["source_format"]]["original_url"]
                documents.update_one(
                    {"_id": ObjectId(row["_id"])},
                    {
                        "$set": {
                            "formats.txt": {
                                "extension": "txt",
                                "original_url": original_url,
                            }
                        }
                    },
                )
                kept_company_ids.add(row["company_id"])
                execution_log.append(
                    {"_id": row["_id"], "decision": decision, "status": "done"}
                )
            elif decision.startswith("delete_"):
                for gcs_path in row.get("gcs_paths_to_delete", []):
                    try:
                        bucket.blob(gcs_path).delete()
                    except Exception as e:
                        print(f"  Failed to delete {gcs_path}: {e}")
                documents.delete_one({"_id": ObjectId(row["_id"])})
                execution_log.append(
                    {"_id": row["_id"], "decision": decision, "status": "done"}
                )
        except Exception as e:
            # A single row's failure (transient Mongo/GCS error, etc.) must
            # not abort a batch this size -- log it and keep going, so a
            # partial failure never leaves the remaining candidates
            # unprocessed with no record of what happened.
            error_count += 1
            print(f"  ERROR processing {row['_id']} ({decision}): {e}")
            execution_log.append(
                {
                    "_id": row["_id"],
                    "decision": decision,
                    "status": "error",
                    "error": str(e),
                }
            )

    log_path = Path("scripts/data/reconstruction_execution_log.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        for entry in execution_log:
            f.write(json.dumps(entry) + "\n")
    print(f"\nWrote execution log to {log_path}")

    print(
        f"Updated/kept {len(kept_company_ids)} companies' worth of new captures "
        f"({error_count} row errors out of {len(rows)})"
    )

    if kept_company_ids:
        CFG.USE_MONGO = True
        CFG.MONGODB_URL = args.mongodb_url
        CFG.DB_NAME = args.db_name
        CFG.USE_GCP = True
        CFG.IN_CLOUD = False
        CFG.GCP_BUCKET = args.gcp_bucket
        CFG.CHANGES_DIR = Path(args.changes_dir)

        print(f"Regenerating changes for {len(kept_company_ids)} companies...")
        create_changes_json(idSet=kept_company_ids)
        print("Regeneration complete")


if __name__ == "__main__":
    main()
