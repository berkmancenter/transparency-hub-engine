# Wayback Machine Historical Backfill - one-shot run for a single company
#
# Runs the same discovery -> fetch -> gate -> dedupe -> store -> .CHANGES.json
# sequence as archive_backfill/main.py, but scoped to ONE company, so a
# single platform can be backfilled on demand without waiting for (or
# interfering with) the scheduled job's queue. Progress is still
# checkpointed in the backfill database exactly like a scheduled run, so
# re-running this picks up any failed/pending snapshots, and the nightly job
# won't redo work this already finished.
#
# A company's current doc_urls often aren't where its policies lived
# historically (e.g. a platform whose policies moved to a parent company's
# help center), so --extra-url adds more seed urls for a doc type. Those are
# only tracked in the backfill database -- the companies collection is never
# modified.
#
# Usage:
#   python archive_backfill/oneshot.py --company-name Fitbit --dry_run true
#   python archive_backfill/oneshot.py --company-name Fitbit \
#       --extra-url privacy_policy=https://www.fitbit.com/legal/privacy-policy \
#       --skip-doc-urls --end-year 2026

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bson import ObjectId  # noqa: E402

from archive_backfill import cdx, companies, db, fetch, pipeline  # noqa: E402
from archive_backfill.settings import CFG, init_settings, logger  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(
        description="One-shot Wayback backfill for a single company"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--company-id", type=str)
    group.add_argument("--company-name", type=str)
    parser.add_argument(
        "--extra-url",
        action="append",
        default=[],
        metavar="DOC_TYPE=URL",
        help="Additional historical seed url for a doc type (repeatable)",
    )
    parser.add_argument(
        "--skip-doc-urls",
        action="store_true",
        help="Only use --extra-url seeds, not the company's current doc_urls",
    )
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--end-year", type=int)
    parser.add_argument(
        "--dry-run-fetch-limit",
        type=int,
        default=3,
        help="Dry runs only: how many discovered snapshots per url to preview-fetch",
    )
    args, _unknown = parser.parse_known_args()
    return args


def find_company(cols: dict, args) -> dict:
    if args.company_id:
        company = cols["companies"].find_one({"_id": ObjectId(args.company_id)})
    else:
        company = cols["companies"].find_one({"name": args.company_name})
    if company is None:
        logger.error(f"No company found for {args.company_id or args.company_name}")
        sys.exit(1)
    return company


def work_units(company: dict, args) -> list[tuple[str, str]]:
    """(doc_type, url) pairs to backfill for this company."""
    units = []
    if not args.skip_doc_urls:
        for doc_type, urls in companies.iter_doc_type_urls(company):
            units.extend((doc_type, url) for url in urls)
    for spec in args.extra_url:
        doc_type, sep, url = spec.partition("=")
        if not sep or not url:
            logger.error(f"--extra-url must look like DOC_TYPE=URL, got {spec!r}")
            sys.exit(1)
        units.append((companies.slugify_doc_type(doc_type), url))
    return units


def dry_run(cols: dict, bucket, company: dict, units: list, preview_limit: int):
    """
    The scheduled pipeline's dry run can't preview anything for work units
    that were never seeded (dry runs never write the progress rows it claims
    from), so run discovery directly here and preview-fetch a few snapshots
    per url. fetch.build_and_store_capture honors CFG.DRY_RUN itself.
    """
    company_id, company_name = str(company["_id"]), company.get("name", "Unknown")
    try:
        for doc_type, url in units:
            try:
                timestamps = cdx.discover(url)
            except cdx.RateLimited:
                raise
            except Exception as e:
                logger.error(f"[DRY RUN] Discovery failed for {url}: {e}")
                continue
            logger.info(
                f"[DRY RUN] {doc_type} {url}: {len(timestamps)} snapshot(s) {timestamps}"
            )
            for ts in timestamps[:preview_limit]:
                status, details = fetch.build_and_store_capture(
                    cols, bucket, company_id, company_name, doc_type, url, ts
                )
                logger.info(f"[DRY RUN]   {ts}: {status} {details}")
    except cdx.RateLimited as e:
        logger.warning(f"[DRY RUN] Stopped early, rate limited by web.archive.org: {e}")


def main():
    args = parse_args()
    init_settings()
    if args.start_year:
        CFG.START_YEAR = args.start_year
    if args.end_year:
        CFG.END_YEAR = args.end_year

    client = db.connect()
    cols = db.get_collections(client)
    db.ensure_indexes(cols)

    bucket = None
    from source.globals import CFG as ROOT_CFG

    if not ROOT_CFG.IN_CLOUD and ROOT_CFG.USE_GCP:
        from google.cloud.storage import Client

        bucket = Client().bucket(ROOT_CFG.GCP_BUCKET)

    try:
        company = find_company(cols, args)
        company_id, company_name = str(company["_id"]), company.get("name", "Unknown")
        units = work_units(company, args)
        logger.info(
            f"One-shot backfill for {company_name} ({company_id}), "
            f"{CFG.START_YEAR}-{CFG.END_YEAR}, {len(units)} url(s):\n"
            + "\n".join(f"  {t}: {u}" for t, u in units)
        )

        if CFG.DRY_RUN:
            dry_run(cols, bucket, company, units, args.dry_run_fetch_limit)
            return

        for doc_type, url in units:
            db.seed_discovery_row(
                cols["discovery_progress"], company_id, company_name, doc_type, url
            )

        budget = pipeline.TimeBudget(CFG.TIME_BUDGET_SECONDS)
        discovered = pipeline.run_discovery_phase(cols, budget, company_id)
        fetched, affected = pipeline.run_fetch_phase(cols, bucket, budget, company_id)

        if affected and CFG.CREATE_CHANGES:
            logger.info(f"Regenerating .CHANGES.json for {company_name}...")
            from source.changes import create_changes_json

            create_changes_json(affected)

        if budget.halt_reason == "rate_limited":
            logger.warning(
                "Stopped early: rate limited by web.archive.org. Re-run later to continue."
            )
        logger.info(
            f"One-shot backfill for {company_name} done: "
            f"{discovered} url(s) discovered, {fetched} snapshot(s) processed, "
            f"new captures stored: {bool(affected)}"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
