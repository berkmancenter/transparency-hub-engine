# Transparency Archiver - Online Document Capture Tool
# Adds new platforms to the live "companies" collection from a reviewed JSON
# file (a JSON array of company records in the same shape as existing
# companies -- see README.md's company schema example).
#
# Every record is validated before anything is written:
#   - id, name, metadata.sns_type, metadata.status and a "Privacy Policy"
#     doc_urls entry are required
#   - doc_urls entries must use the {"name": ..., "urls": [...]} shape with
#     absolute http(s) urls and no tracking query params (utm_*, gclid, sjid,
#     ...), which would make every daily capture look like a different page
#   - neither the id nor the name may already exist in the collection (or
#     appear twice in the file)
# Review-only fields (any top-level key starting with "_", e.g. "_review")
# are stripped before insert.
#
# Records are only ever inserted, never updated -- existing companies are
# untouched. New records get fresh ObjectIds. Note that the live pipeline's
# daily runs only crawl companies listed in config.yaml's PRIORITY_DICT; new
# companies are picked up by the weekly SYNC_PRIORITY=1 run.
#
# Defaults to a DRY RUN: prints what WOULD be inserted without writing.
# Pass --confirm to actually insert.
#
# Usage:
#   python scripts/add_companies.py --input new_companies.json             # dry run
#   python scripts/add_companies.py --input new_companies.json --confirm   # for real

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

DEFAULT_DB_NAME = "transparencydb_dev"
TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gclid|fbclid|msclkid|sjid|ref|ref_src|ref_topic|mc_.*|_ga|igshid)$",
    re.IGNORECASE,
)
ID_PATTERN = re.compile(r"^[a-z0-9_]+$")


def validate_record(record: dict) -> list[str]:
    """Returns a list of problems with one company record (empty = valid)."""
    problems = []
    if not ID_PATTERN.match(str(record.get("id", ""))):
        problems.append(f"bad id {record.get('id')!r} (lowercase slug expected)")
    if not record.get("name"):
        problems.append("missing name")
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        problems.append("missing metadata")
    else:
        if not isinstance(metadata.get("sns_type"), str):
            problems.append("metadata.sns_type must be a string")
        if metadata.get("status") not in ("Active", "Inactive"):
            problems.append("metadata.status must be Active or Inactive")

    doc_urls = record.get("doc_urls")
    if not isinstance(doc_urls, dict) or "Privacy Policy" not in doc_urls:
        problems.append("doc_urls must include a Privacy Policy")
        return problems
    for doc_type, entry in doc_urls.items():
        if not isinstance(entry, dict) or not entry.get("urls"):
            problems.append(f"{doc_type}: expected {{'name', 'urls': [...]}}")
            continue
        for url in entry["urls"]:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                problems.append(f"{doc_type}: not an absolute url: {url}")
            tracking = [
                k for k, _ in parse_qsl(parsed.query) if TRACKING_PARAMS.match(k)
            ]
            if tracking:
                problems.append(f"{doc_type}: tracking params {tracking} in {url}")
        if len(set(entry["urls"])) != len(entry["urls"]):
            problems.append(f"{doc_type}: duplicate urls")
    return problems


def clean_record(record: dict) -> dict:
    """Drops review-only fields (top-level keys starting with "_")."""
    return {k: v for k, v in record.items() if not k.startswith("_")}


def main():
    parser = argparse.ArgumentParser(
        description="Insert reviewed new companies into the live companies "
        "collection. Defaults to a dry run; pass --confirm to actually insert."
    )
    parser.add_argument("--input", required=True, help="JSON array of company records")
    parser.add_argument("--mongodb-url", default=os.getenv("MONGODB_URL"))
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually insert. Without this flag, only a dry-run report is produced.",
    )
    parser.add_argument("--log-output", default="scripts/data/add_companies_log.jsonl")
    args = parser.parse_args()

    if not args.mongodb_url:
        parser.error("--mongodb-url is required (or set MONGODB_URL in .env)")

    from pymongo import MongoClient
    from pymongo.server_api import ServerApi

    with open(args.input, "r", encoding="utf-8") as f:
        records = json.load(f)
    print(f"Loaded {len(records)} record(s) from {args.input}")

    client = MongoClient(
        args.mongodb_url,
        server_api=ServerApi("1", strict=True, deprecation_errors=True),
    )
    companies = client[args.db_name]["companies"]
    existing_ids = set()
    existing_names = set()
    for company in companies.find({}, {"id": 1, "name": 1}):
        existing_ids.add(str(company.get("id", "")).lower())
        existing_names.add(str(company.get("name", "")).strip().lower())

    all_problems = {}
    seen_ids, seen_names = set(), set()
    for record in records:
        key = record.get("id") or record.get("name") or "?"
        problems = validate_record(record)
        rid = str(record.get("id", "")).lower()
        rname = str(record.get("name", "")).strip().lower()
        if rid in existing_ids or rname in existing_names:
            problems.append("already exists in companies (by id or name)")
        if rid in seen_ids or rname in seen_names:
            problems.append("duplicated within the input file")
        seen_ids.add(rid)
        seen_names.add(rname)
        if problems:
            all_problems[key] = problems

    if all_problems:
        print(f"\n{len(all_problems)} record(s) failed validation -- nothing written:")
        for key, problems in all_problems.items():
            for problem in problems:
                print(f"  {key}: {problem}")
        sys.exit(1)

    cleaned = [clean_record(r) for r in records]
    doc_count = sum(len(r["doc_urls"]) for r in cleaned)
    print(f"All {len(cleaned)} record(s) valid ({doc_count} doc_urls entries)")

    if not args.confirm:
        for r in cleaned:
            print(
                f"  [DRY RUN] would insert {r['name']} ({r['id']}): {', '.join(r['doc_urls'])}"
            )
        print("\nThis was a dry run. Re-run with --confirm to insert.")
        return

    result = companies.insert_many(cleaned)
    log_output = Path(args.log_output)
    log_output.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with open(log_output, "a", encoding="utf-8") as f:
        for record, inserted_id in zip(cleaned, result.inserted_ids):
            f.write(
                json.dumps(
                    {
                        "inserted_at": stamp,
                        "_id": str(inserted_id),
                        "id": record["id"],
                        "name": record["name"],
                    }
                )
                + "\n"
            )
    print(
        f"Inserted {len(result.inserted_ids)} compan(y/ies); log appended to {log_output}"
    )


if __name__ == "__main__":
    main()
