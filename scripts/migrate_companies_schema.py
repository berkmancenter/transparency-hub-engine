# Transparency Archiver - Online Document Capture Tool
# One-off migration: replaces the live "companies" collection with an
# already-migrated export that converts every doc_urls entry from the old
# bare-string shape ({"Privacy Policy": "https://..."}) to the new
# multi-url shape ({"Privacy Policy": {"name": ..., "urls": [...]}})
# introduced by commit 7445666 ("Support multi-URL doc types"). The live
# companies collection was never actually migrated when that commit landed,
# which meant source/utils.py's format_data() -- and, by extension, the
# whole live pipeline -- would raise on every real company record.
#
# On top of the schema conversion, this also applies a small hand-reviewed
# set of doc_urls content changes recorded in "policy wide links.txt" (a
# human-curated list of additional/corrected policy sub-pages for specific
# companies, using the new multi-url support). See DOC_URLS_PATCH below --
# it's hand-encoded rather than parsed from the .txt file at runtime,
# because that file's format (blank-line-separated url groups, "NEW LINK"
# and "REMOVE <url>" annotations) is informal, one-off human notation, not
# a stable machine format; each entry here is the resolved, reviewed
# outcome of reading that file, matching every group to a company/doc_type
# by domain, and confirming the ambiguous ones (Badoo's removal semantics,
# whether Tumblr/Yubo's new urls append or replace, whether Viber's new url
# replaces its Terms of Service entry) with a human before writing anything.
#
# Safety:
#   - Refuses to run unless the live "companies" collection's _id set
#     exactly matches the migrated file's -- this migration only ever
#     replaces existing companies in place, never adds/removes any, so any
#     mismatch means the live db has diverged from what this script assumes
#     and it's not safe to proceed blindly.
#   - Always backs up the live "companies" collection (as mongoexport-
#     compatible extended JSON) before writing, confirm or not.
#   - Defaults to a DRY RUN: prints a full summary of what would change
#     (schema conversion counts, non-doc_urls field diffs, and every
#     DOC_URLS_PATCH change) without writing anything. Pass --confirm to
#     actually replace the collection.
#
# Usage:
#   python scripts/migrate_companies_schema.py
#   python scripts/migrate_companies_schema.py --confirm

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bson import json_util  # noqa: E402

DEFAULT_MIGRATED_FILE = "transparencydb_dev.companies090826.migrated.json"
DEFAULT_DB_NAME = "transparencydb_dev"
DEFAULT_BACKUP_DIR = "scripts/data"

# Resolved from "policy wide links.txt" -- see module docstring. Each entry:
#   company: the company's "id" slug (stable, human-readable identifier)
#   doc_type: the doc_urls display-name key to modify
#   action:
#     "replace_urls" -- set the doc_type's urls list to `urls` outright
#       (used where the .txt file's group is that doc_type's full set of
#       sub-pages, starting with the url already on file)
#     "append_urls" -- add `urls` to whatever's already there, skipping any
#       url already present (used where the .txt file marked new urls
#       without removing the existing one)
#     "delete_doc_type" -- remove the doc_type entry entirely (used for
#       Badoo's explicit "REMOVE" line, which left its "Safety" doc_type
#       with no urls at all)
DOC_URLS_PATCH = [
    {
        "company": "tiktok",
        "doc_type": "Community Guidelines",
        "action": "replace_urls",
        "urls": [
            "https://www.tiktok.com/safety/en/policies-and-engagement/overview",
            "https://www.tiktok.com/safety/en/policies-and-engagement/community-principles",
            "https://www.tiktok.com/safety/en/policies-and-engagement/youth-safety",
            "https://www.tiktok.com/safety/en/policies-and-engagement/safety-civility",
            "https://www.tiktok.com/safety/en/policies-and-engagement/mental-behavioral-health",
            "https://www.tiktok.com/safety/en/policies-and-engagement/sensitive-mature-themes",
            "https://www.tiktok.com/safety/en/policies-and-engagement/integrity-authenticity",
            "https://www.tiktok.com/safety/en/policies-and-engagement/regulated-commercial-activities",
            "https://www.tiktok.com/safety/en/policies-and-engagement/privacy-security",
            "https://www.tiktok.com/safety/en/policies-and-engagement/fyf-standards",
            "https://www.tiktok.com/safety/en/policies-and-engagement/accounts-features",
            "https://www.tiktok.com/safety/en/policies-and-engagement/enforcement",
        ],
    },
    {
        "company": "tiktok",
        "doc_type": "Transparency Report",
        "action": "replace_urls",
        "urls": [
            "https://www.tiktok.com/safety/en/transparency",
            "https://www.tiktok.com/safety/en/transparency/reports",
            "https://www.tiktok.com/safety/en/transparency/cg-report",
            "https://www.tiktok.com/safety/en/transparency/government-removals-report",
            "https://www.tiktok.com/safety/en/transparency/information-requests-report",
            "https://www.tiktok.com/safety/en/transparency/intellectual-property-report",
            "https://www.tiktok.com/safety/en/transparency/covert-influence-operations",
            "https://www.tiktok.com/safety/en/transparency/child-exploitation",
            "https://www.tiktok.com/safety/en/transparency/platform-security",
        ],
    },
    {
        "company": "linkedin",
        "doc_type": "Transparency Report",
        "action": "replace_urls",
        "urls": [
            "https://about.linkedin.com/transparency",
            "https://about.linkedin.com/transparency/community-report",
            "https://about.linkedin.com/transparency/government-requests-report",
        ],
    },
    {
        "company": "github",
        "doc_type": "Transparency Report",
        "action": "replace_urls",
        "urls": [
            "https://transparencycenter.github.com/",
            "https://transparencycenter.github.com/user-information/",
            "https://transparencycenter.github.com/government/",
            "https://transparencycenter.github.com/dmca/",
            "https://transparencycenter.github.com/automated-detection/",
            "https://transparencycenter.github.com/appeals/",
        ],
    },
    {
        "company": "tumblr",
        "doc_type": "Transparency Report",
        "action": "append_urls",
        "urls": [
            "https://transparency.automattic.com/reports/tumblr/",
            "https://transparency.automattic.com/transparency-report/tumblr-transparency-report-jan-jun-2026/",
        ],
    },
    {
        "company": "yubo",
        "doc_type": "Transparency Report",
        "action": "append_urls",
        "urls": [
            "https://yubo.cdn.prismic.io/yubo/aZ8Xa8FoBIGEgyLr_Transparency-Report_February-2026_Eng.pdf",
        ],
    },
    {
        "company": "badoo",
        "doc_type": "Safety",
        "action": "delete_doc_type",
    },
    {
        "company": "rakuten_viber",
        "doc_type": "Terms of Service",
        "action": "replace_urls",
        "urls": [
            "https://www.viber.com/en/terms/viber-terms-use/",
        ],
    },
]


def load_migrated_companies(path: str) -> list[dict]:
    """
    Loads a mongoexport --jsonArray file, converting EVERY extended-JSON
    type ($oid, $date, $numberLong, ...) back into real BSON values so the
    result can be written straight to Mongo. Uses bson.json_util.loads for
    this rather than a plain json.loads() + hand-picked "_id.$oid" fixup --
    today's export only has extended-JSON on _id, but a future export could
    just as easily gain a $date field (e.g. a created_at/updated_at) that a
    narrower conversion would silently leave as a literal {"$date": ...}
    dict instead of a real datetime, and bulk_write would write that
    corrupted value straight into the live collection with nothing to catch it.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return json_util.loads(raw)


def apply_doc_urls_patch(companies: list[dict], patch: list[dict]) -> list[dict]:
    """Applies DOC_URLS_PATCH to the in-memory migrated companies, matching
    by the company's "id" slug. Returns a list of human-readable change
    descriptions for the dry-run summary / log."""
    by_id = {c.get("id"): c for c in companies}
    changes = []

    for entry in patch:
        company = by_id.get(entry["company"])
        if company is None:
            changes.append(
                f"SKIPPED (company id '{entry['company']}' not found): {entry}"
            )
            continue

        doc_urls = company.setdefault("doc_urls", {})
        doc_type = entry["doc_type"]
        action = entry["action"]
        label = f"{company.get('name', entry['company'])} / {doc_type}"

        if action == "delete_doc_type":
            if doc_type in doc_urls:
                del doc_urls[doc_type]
                changes.append(f"{label}: deleted doc_type entry")
            else:
                changes.append(f"{label}: SKIPPED delete, doc_type not present")
            continue

        existing = doc_urls.get(doc_type)
        existing_urls = existing.get("urls", []) if isinstance(existing, dict) else []

        if action == "replace_urls":
            doc_urls[doc_type] = {"name": doc_type, "urls": entry["urls"]}
            changes.append(
                f"{label}: {len(existing_urls)} url(s) -> {len(entry['urls'])} url(s)"
            )
        elif action == "append_urls":
            merged = existing_urls + [
                u for u in entry["urls"] if u not in existing_urls
            ]
            doc_urls[doc_type] = {"name": doc_type, "urls": merged}
            added = len(merged) - len(existing_urls)
            changes.append(
                f"{label}: appended {added} new url(s), now {len(merged)} total"
            )
        else:
            changes.append(f"{label}: SKIPPED unknown action '{action}'")

    return changes


def backup_live_companies(collection, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(
        backup_dir, f"companies_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    )
    docs = list(collection.find())
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(json_util.dumps(docs, indent=2))
    return backup_path


def summarize_non_doc_urls_diffs(live_by_id: dict, migrated_by_id: dict) -> int:
    diffs = 0
    for cid, migrated in migrated_by_id.items():
        live = live_by_id.get(cid)
        if live is None:
            continue
        for key in set(live.keys()) | set(migrated.keys()):
            if key in ("doc_urls", "_id"):
                continue
            if live.get(key) != migrated.get(key):
                diffs += 1
    return diffs


def main():
    parser = argparse.ArgumentParser(
        description="Replace the live companies collection with the doc_urls-schema-migrated "
        "export, plus the hand-reviewed 'policy wide links.txt' url updates. Defaults to a dry "
        "run; pass --confirm to actually write."
    )
    parser.add_argument("--migrated-file", default=DEFAULT_MIGRATED_FILE)
    parser.add_argument("--mongodb-url", default=os.getenv("MONGODB_URL"))
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--skip-patch",
        action="store_true",
        help="Only apply the doc_urls schema conversion, skip the policy wide links.txt patch",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually replace the live collection. Without this flag, only a dry-run summary is printed.",
    )
    args = parser.parse_args()

    if not args.mongodb_url:
        parser.error("--mongodb-url is required (or set MONGODB_URL in .env)")

    dry_run = not args.confirm

    from pymongo import MongoClient
    from pymongo.server_api import ServerApi

    client = MongoClient(
        args.mongodb_url,
        server_api=ServerApi("1", strict=True, deprecation_errors=True),
    )
    companies_col = client[args.db_name]["companies"]

    migrated = load_migrated_companies(args.migrated_file)
    print(f"Loaded {len(migrated)} companies from {args.migrated_file}")

    patch_changes = []
    if not args.skip_patch:
        patch_changes = apply_doc_urls_patch(migrated, DOC_URLS_PATCH)

    live_docs = list(companies_col.find())
    live_by_id = {doc["_id"]: doc for doc in live_docs}
    migrated_by_id = {doc["_id"]: doc for doc in migrated}

    live_ids = set(live_by_id)
    migrated_ids = set(migrated_by_id)
    if live_ids != migrated_ids:
        only_live = live_ids - migrated_ids
        only_migrated = migrated_ids - live_ids
        print(
            f"ABORTING: live companies collection has diverged from {args.migrated_file} -- "
            f"{len(only_live)} id(s) only in live, {len(only_migrated)} id(s) only in the migrated file. "
            "This script only replaces existing companies in place; resolve the mismatch before re-running."
        )
        client.close()
        sys.exit(1)

    print(
        f"Verified {len(live_ids)} companies match 1:1 between live and {args.migrated_file}"
    )

    non_doc_urls_diffs = summarize_non_doc_urls_diffs(live_by_id, migrated_by_id)
    print(
        f"Non-doc_urls field changes (metadata/url cleanup already baked into the export): {non_doc_urls_diffs}"
    )

    print(f"\npolicy wide links.txt patch ({len(patch_changes)} change(s)):")
    for change in patch_changes:
        print(f"  {change}")

    if dry_run:
        print(
            "\n=== DRY RUN -- nothing was written. Pass --confirm to actually replace the collection. ==="
        )
        client.close()
        return

    backup_path = backup_live_companies(companies_col, args.backup_dir)
    print(f"\nBacked up {len(live_docs)} live companies to {backup_path}")

    from pymongo import ReplaceOne

    operations = [ReplaceOne({"_id": doc["_id"]}, doc) for doc in migrated]
    result = companies_col.bulk_write(operations, ordered=True)
    print(
        f"Replaced {result.matched_count} companies ({result.modified_count} actually changed)"
    )

    client.close()


if __name__ == "__main__":
    main()
