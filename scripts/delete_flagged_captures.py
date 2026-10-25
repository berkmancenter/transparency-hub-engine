# Transparency Archiver - Online Document Capture Tool
# Deletes the bad (non-policy) captures identified by
# scripts/find_bad_captures.py from both the live documents_v2 collection
# and GCS, then cleans up and regenerates the .CHANGES.json comparison data
# that referenced them.
#
# A flagged document is frequently one side of a diff pair in the "changes"
# collection (used by the Transparency Hub's Comparison Tool webapp). This
# script:
#   1. Deletes every flagged document's GCS files (all formats) and its
#      documents_v2 record.
#   2. Deletes every "changes" record that referenced a flagged document
#      (as either older_document_id or newer_document_id) along with its
#      .CHANGES.json file in GCS.
#   3. Re-runs source.changes.create_changes_json() for the affected
#      companies, so the Comparison Tool gets a clean diff chain across
#      whatever valid snapshots remain.
#
# Defaults to a DRY RUN: it prints and logs everything it WOULD do without
# touching Mongo or GCS. Pass --confirm to actually execute the deletions
# and regeneration.
#
# Usage:
#   python scripts/delete_flagged_captures.py                 # dry run
#   python scripts/delete_flagged_captures.py --confirm        # for real

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv(dotenv_path=".env")

DEFAULT_DB_NAME = "transparencydb_dev"
DEFAULT_GCP_BUCKET = "transparency_hub_documents"
DEFAULT_CHANGES_DIR = "changes"


def load_jsonl(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def find_affected_changes(changes_collection, doc_ids: list[ObjectId]) -> list[dict]:
    query = {
        "$or": [
            {"older_document_id": {"$in": doc_ids}},
            {"newer_document_id": {"$in": doc_ids}},
        ]
    }
    return list(changes_collection.find(query))


def delete_gcs_blob(bucket, path: str, dry_run: bool, log: list) -> None:
    if dry_run:
        log.append({"action": "delete_gcs_blob", "path": path, "status": "dry_run"})
        return
    try:
        bucket.blob(path).delete()
        log.append({"action": "delete_gcs_blob", "path": path, "status": "deleted"})
    except Exception as e:
        # google.cloud.exceptions.NotFound is the common case (already gone);
        # any other failure is logged the same way so the run isn't aborted
        # partway through a 90-document batch over one missing blob.
        log.append(
            {
                "action": "delete_gcs_blob",
                "path": path,
                "status": "error",
                "error": str(e),
            }
        )


def main():
    parser = argparse.ArgumentParser(
        description="Delete flagged bad captures (docs + GCS files) and "
        "regenerate the changes data that referenced them. Defaults to a "
        "dry run; pass --confirm to actually execute."
    )
    parser.add_argument("--report", default="scripts/data/bad_captures_report.jsonl")
    parser.add_argument("--mongodb-url", default=os.getenv("MONGODB_URL"))
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--gcp-bucket", default=DEFAULT_GCP_BUCKET)
    parser.add_argument("--changes-dir", default=DEFAULT_CHANGES_DIR)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually perform the deletions and regeneration. Without this "
        "flag, only a dry-run report is produced.",
    )
    parser.add_argument("--log-output", default="scripts/data/deletion_log.jsonl")
    args = parser.parse_args()

    if not args.mongodb_url:
        parser.error("--mongodb-url is required (or set MONGODB_URL in .env)")

    dry_run = not args.confirm

    from google.cloud.storage import Client
    from pymongo import MongoClient
    from pymongo.server_api import ServerApi

    flagged_rows = load_jsonl(Path(args.report))
    print(f"Loaded {len(flagged_rows)} flagged documents from {args.report}")

    mongo_client = MongoClient(
        args.mongodb_url,
        server_api=ServerApi("1", strict=True, deprecation_errors=True),
    )
    documents = mongo_client[args.db_name]["documents_v2"]
    changes = mongo_client[args.db_name]["changes"]

    storage_client = Client()
    bucket = storage_client.bucket(args.gcp_bucket)

    doc_object_ids = [ObjectId(row["_id"]) for row in flagged_rows]
    affected_changes = find_affected_changes(changes, doc_object_ids)
    affected_company_ids = sorted({row["company_id"] for row in flagged_rows})

    print(f"Found {len(affected_changes)} change records referencing these documents")
    print(
        f"Affected companies (will have changes regenerated): {len(affected_company_ids)}"
    )

    if dry_run:
        print("\n=== DRY RUN — nothing will be deleted or modified ===\n")
    else:
        print("\n=== LIVE RUN — deletions are permanent ===\n")

    log: list[dict] = []
    run_started_at = datetime.now(timezone.utc).isoformat()

    # 1. Delete each flagged document's GCS files and Mongo record.
    for row in flagged_rows:
        for gcs_path in row["gcs_paths_to_delete"]:
            delete_gcs_blob(bucket, gcs_path, dry_run, log)

        if dry_run:
            log.append(
                {
                    "action": "delete_mongo_document",
                    "_id": row["_id"],
                    "status": "dry_run",
                }
            )
        else:
            result = documents.delete_one({"_id": ObjectId(row["_id"])})
            log.append(
                {
                    "action": "delete_mongo_document",
                    "_id": row["_id"],
                    "status": "deleted" if result.deleted_count else "not_found",
                }
            )

    print(f"Processed {len(flagged_rows)} flagged documents")

    # 2. Delete each affected change record's GCS file and Mongo record.
    for change in affected_changes:
        change_id = str(change["_id"])
        gcs_path = change["path"]
        delete_gcs_blob(bucket, gcs_path, dry_run, log)

        if dry_run:
            log.append(
                {
                    "action": "delete_change_record",
                    "_id": change_id,
                    "status": "dry_run",
                }
            )
        else:
            result = changes.delete_one({"_id": change["_id"]})
            log.append(
                {
                    "action": "delete_change_record",
                    "_id": change_id,
                    "status": "deleted" if result.deleted_count else "not_found",
                }
            )

    print(f"Processed {len(affected_changes)} affected change records")

    # 3. Regenerate changes for the affected companies from whatever valid
    # snapshots remain. Only meaningful (and only run) on a real execution --
    # there's nothing to regenerate against in a dry run since nothing was
    # actually deleted.
    if not dry_run:
        from source.globals import CFG
        from source.changes import create_changes_json

        CFG.USE_MONGO = True
        CFG.MONGODB_URL = args.mongodb_url
        CFG.DB_NAME = args.db_name
        CFG.USE_GCP = True
        CFG.IN_CLOUD = False
        CFG.GCP_BUCKET = args.gcp_bucket
        CFG.CHANGES_DIR = Path(args.changes_dir)

        print(f"\nRegenerating changes for {len(affected_company_ids)} companies...")
        create_changes_json(idSet=set(affected_company_ids))
        log.append(
            {
                "action": "regenerate_changes",
                "company_ids": affected_company_ids,
                "status": "done",
            }
        )
        print("Regeneration complete")
    else:
        print(
            "\n(Skipping changes regeneration in dry run -- would regenerate "
            f"for {len(affected_company_ids)} companies on a real run)"
        )

    mongo_client.close()

    log_output = Path(args.log_output)
    log_output.parent.mkdir(parents=True, exist_ok=True)
    with open(log_output, "w", encoding="utf-8") as f:
        f.write(
            json.dumps({"run_started_at": run_started_at, "dry_run": dry_run}) + "\n"
        )
        for entry in log:
            f.write(json.dumps(entry) + "\n")
    print(f"\nWrote audit log to {log_output}")

    if dry_run:
        print(
            "\nThis was a dry run. Re-run with --confirm to actually delete "
            "these documents, delete the affected change records, and "
            "regenerate changes for the affected companies."
        )


if __name__ == "__main__":
    main()
