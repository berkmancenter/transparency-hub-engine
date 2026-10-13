# Wayback Machine Historical Backfill - mark companies as already covered
#
# Some companies already have historical Wayback data captured through
# effort outside this job (a manual run of the old scripts/web_archive_capture.py,
# a one-off import, ...). Running the backfill job against them again would
# re-discover and re-fetch the same snapshots for no benefit. This script
# marks a company's work units in discovery_progress as "already_captured"
# so seed_discovery_progress's own seeding (which only ever inserts rows
# that don't exist yet -- see db.seed_discovery_row) leaves them alone, and
# claim_discovery_batch never selects them (see db.py's _claim_batch query --
# "already_captured" isn't one of the statuses it matches). Any fetch_progress
# rows already queued up for that company (if discovery had already run
# before you realized it was redundant) are swept to "skipped_precaptured"
# too, so nothing already queued gets downloaded either.
#
# This is metadata-only: it never touches documents_v2 or GCS, and it's
# fully reversible by re-running seed_discovery_progress after deleting the
# affected discovery_progress rows (or re-marking them some other status)
# if a company turns out to need backfilling after all.
#
# Defaults to a DRY RUN: prints what WOULD be marked without writing
# anything. Pass --confirm to actually write.
#
# Usage:
#   # By Mongo _id (the same id documents_v2/companies use) -- marks EVERY
#   # doc_type on the company
#   python archive_backfill/mark_precaptured.py --company-id 64f1a2b3c4d5e6f7a8b9c0d1 --confirm
#
#   # By exact company name
#   python archive_backfill/mark_precaptured.py --company-name "Acme Corp" --confirm
#
#   # From a file, one company _id or exact name per line
#   python archive_backfill/mark_precaptured.py --file already_backfilled.txt --confirm
#
#   # Every company currently in the companies collection (rare -- e.g. a full
#   # manual backfill was done once and this job should start with everything skipped)
#   python archive_backfill/mark_precaptured.py --all --confirm
#
#   # Precise per-(company, doc_type) marking, from a "company_id,doc_type"
#   # CSV -- see extract_precaptured_from_export.py, which derives exactly
#   # this file from an existing documents_v2 export
#   python archive_backfill/mark_precaptured.py --pairs-file archive_backfill/data/precaptured_pairs.csv --confirm
#
# Always attach --note so it's clear later WHY a company was skipped:
#   python archive_backfill/mark_precaptured.py --company-name "Acme Corp" \
#       --note "Manually backfilled 2026-08-04, see transparencydb_dev.documents08042026.json" --confirm

import argparse
import sys
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId

# See archive_backfill/main.py for why this is needed when running this
# file directly rather than via `python -m archive_backfill.mark_precaptured`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archive_backfill import companies as companies_module  # noqa: E402
from archive_backfill import db  # noqa: E402
from archive_backfill.settings import init_settings, logger  # noqa: E402


def resolve_companies(cols, args) -> list:
    """Returns the list of company documents to mark, based on the CLI args given."""
    companies_col = cols["companies"]

    if args.all:
        return list(companies_col.find())

    identifiers = list(args.company_id or []) + list(args.company_name or [])
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            identifiers += [line.strip() for line in f if line.strip()]

    resolved = []
    for identifier in identifiers:
        company = None
        try:
            company = companies_col.find_one({"_id": ObjectId(identifier)})
        except InvalidId:
            pass
        if company is None:
            company = companies_col.find_one({"name": identifier})
        if company is None:
            logger.warning(
                f"Could not find a company matching '{identifier}' by _id or name, skipping"
            )
            continue
        resolved.append(company)
    return resolved


def find_url_for_doc_type(company: dict, doc_type: str) -> str | None:
    """
    doc_type here is the slugified form ("privacy_policy") used throughout
    discovery_progress/fetch_progress/documents_v2's own "type" field. Finds
    the doc_urls entry that slugifies to doc_type and returns its first url
    -- see archive_backfill/companies.py for why this tolerates both the old
    (bare url string) and newer ({"name": ..., "urls": [...]}) doc_urls shapes.
    """
    urls = companies_module.find_urls_for_doc_type(company, doc_type)
    return urls[0] if urls else None


def mark_one(
    cols,
    company_id: str,
    company_name: str,
    doc_type: str,
    url: str,
    note: str,
    dry_run: bool,
) -> None:
    if dry_run:
        logger.info(
            f"[DRY RUN] Would mark {company_name} / {doc_type} / {url} as already_captured"
        )
        return
    db.mark_discovery_precaptured(
        cols["discovery_progress"], company_id, company_name, doc_type, url, note=note
    )
    swept = db.mark_fetch_rows_precaptured(cols["fetch_progress"], company_id, doc_type)
    if swept:
        logger.info(
            f"Swept {swept} queued fetch_progress row(s) for {company_name} / {doc_type}"
        )


def mark_company(cols, company: dict, note: str, dry_run: bool) -> int:
    """
    Marks every single-url doc_type on this company as precaptured. Returns
    the number of (doc_type, url) work units marked. Multi-url doc types are
    marked too (for a consistent, audit-able note), even though they were
    already unclaimed as "skipped_multi_url".
    """
    company_id = str(company.get("_id"))
    company_name = company.get("name", "Unknown")
    marked = 0

    for doc_type, urls in companies_module.iter_doc_type_urls(company):
        mark_one(cols, company_id, company_name, doc_type, urls[0], note, dry_run)
        marked += 1

    return marked


def mark_pairs(cols, pairs: list, note: str, dry_run: bool) -> int:
    """
    Marks specific (company_id, doc_type) pairs -- e.g. from
    extract_precaptured_from_export.py -- rather than every doc_type on a
    company. Looks up each pair's url from the companies collection's
    current doc_urls, so a pair referencing a doc_type the company no longer
    has (or never had) is skipped with a warning rather than silently
    creating a bogus progress row.
    """
    company_cache: dict = {}
    marked = 0
    for company_id, doc_type in pairs:
        if company_id not in company_cache:
            try:
                company_cache[company_id] = cols["companies"].find_one(
                    {"_id": ObjectId(company_id)}
                )
            except InvalidId:
                company_cache[company_id] = None
        company = company_cache[company_id]
        if company is None:
            logger.warning(
                f"Company {company_id} not found, skipping pair ({company_id}, {doc_type})"
            )
            continue

        company_name = company.get("name", "Unknown")
        url = find_url_for_doc_type(company, doc_type)
        if url is None:
            logger.warning(
                f"{company_name} ({company_id}) has no doc_urls entry matching doc_type '{doc_type}', skipping"
            )
            continue

        mark_one(cols, company_id, company_name, doc_type, url, note, dry_run)
        marked += 1

    return marked


def parse_pairs_file(path: str) -> list:
    pairs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            company_id, _, doc_type = line.partition(",")
            company_id, doc_type = company_id.strip(), doc_type.strip()
            if company_id and doc_type:
                pairs.append((company_id, doc_type))
    return pairs


def main():
    parser = argparse.ArgumentParser(
        description="Mark companies as already covered by prior Wayback backfill effort, "
        "so archive_backfill's discovery/fetch phases skip them."
    )
    parser.add_argument(
        "--company-id", action="append", help="Mongo _id of a company (repeatable)"
    )
    parser.add_argument(
        "--company-name", action="append", help="Exact company name (repeatable)"
    )
    parser.add_argument(
        "--file", type=str, help="Path to a file with one company _id or name per line"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Mark every company currently in the companies collection",
    )
    parser.add_argument(
        "--pairs-file",
        type=str,
        help="Path to a 'company_id,doc_type' CSV for precise per-doc_type marking "
        "(see extract_precaptured_from_export.py)",
    )
    parser.add_argument(
        "--note",
        type=str,
        default="",
        help="Stored on each marked row for later auditing",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually write the marks. Without this flag, only logs what would be marked (dry run).",
    )
    args = parser.parse_args()

    if not (
        args.company_id or args.company_name or args.file or args.all or args.pairs_file
    ):
        parser.error(
            "Specify at least one of --company-id, --company-name, --file, --all, or --pairs-file"
        )

    init_settings()
    dry_run = not args.confirm
    if dry_run:
        logger.info(
            "=== DRY RUN -- nothing will be written. Pass --confirm to actually mark. ==="
        )

    client = db.connect()
    cols = db.get_collections(client)
    db.ensure_indexes(cols)

    try:
        total_marked = 0

        if args.company_id or args.company_name or args.file or args.all:
            companies = resolve_companies(cols, args)
            logger.info(
                f"Resolved {len(companies)} compan(y/ies) to mark (every doc_type on each)"
            )
            for company in companies:
                total_marked += mark_company(cols, company, args.note, dry_run)

        if args.pairs_file:
            pairs = parse_pairs_file(args.pairs_file)
            logger.info(f"Loaded {len(pairs)} pair(s) from {args.pairs_file}")
            total_marked += mark_pairs(cols, pairs, args.note, dry_run)

        logger.info(
            f"{'Would mark' if dry_run else 'Marked'} {total_marked} work unit(s) as already_captured"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
