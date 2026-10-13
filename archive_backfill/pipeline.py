# Wayback Machine Historical Backfill - orchestration
#
# Two independently-resumable phases, run in sequence within a single job
# execution:
#
#   Discovery phase: for every (company, doc_type, url) work unit not yet
#     discovered, queries the CDX API for its available snapshots and seeds
#     one fetch_progress row per selected timestamp.
#
#   Fetch phase: for every pending fetch_progress row, downloads that one
#     snapshot, quality-gates and dedupes it, and (if it survives both)
#     stores it as a new documents_v2 record.
#
# Every row transition is written to Mongo as it happens (see
# archive_backfill/db.py), so a run that's stopped by CFG.TIME_BUDGET_SECONDS,
# a batch limit, or Cloud Run's own task timeout leaves nothing in an
# ambiguous state -- the next scheduled execution just claims whatever is
# still pending/eligible-for-retry and continues. This replaces the original
# script's local JSON checkpoint files, which didn't survive between
# separate runs of the script.

import sys
import time
from pathlib import Path

from archive_backfill import cdx, companies, db, fetch
from archive_backfill.settings import CFG, logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source.utils import notify_slack  # noqa: E402

# How many rows a single Mongo claim pulls at a time when CFG's own batch
# limit is unset (0 = unlimited) -- just a chunk size for the claim loop,
# not a cap on total work done in a run.
CLAIM_CHUNK_SIZE = 200


class TimeBudget:
    """
    When the run should stop: its time budget ran out, or halt() was called
    (e.g. web.archive.org rate limited us -- see cdx.RateLimited).
    """

    def __init__(self, seconds: int):
        self.deadline = time.monotonic() + seconds if seconds else None
        self.halt_reason: str | None = None

    def halt(self, reason: str) -> None:
        self.halt_reason = reason

    def exhausted(self) -> bool:
        if self.halt_reason is not None:
            return True
        return self.deadline is not None and time.monotonic() >= self.deadline


def _halt_on_rate_limit(budget: TimeBudget, e: Exception) -> None:
    logger.warning(
        f"Rate limited by web.archive.org, ending this run early -- the next run picks up from here: {e}"
    )
    budget.halt("rate_limited")


def _release_unattempted(collection, batch: list, attempted: int) -> None:
    """Releases claimed rows this run stopped before reaching (see db.release_claim)."""
    if CFG.DRY_RUN:
        return
    for row in batch[attempted:]:
        db.release_claim(collection, row)


def seed_discovery_progress(cols: dict) -> None:
    """
    Ensures a discovery_progress row exists for every (company, doc_type,
    url) the companies collection currently knows about. Idempotent -- safe
    to call at the start of every execution; existing rows (and their
    progress) are left untouched.
    """
    if CFG.DRY_RUN:
        logger.info(
            "[DRY RUN] Skipping discovery_progress seeding -- dry runs never write to Mongo"
        )
        return

    logger.info("Seeding discovery_progress from the companies collection...")
    seeded, skipped_multi = 0, 0
    for company in cols["companies"].find():
        company_id = str(company.get("_id"))
        company_name = company.get("name", "Unknown")
        for doc_type, urls in companies.iter_doc_type_urls(company):
            if len(urls) > 1:
                # See module docstring in archive_backfill/pipeline.py --
                # multi-url doc types aren't backfilled automatically in
                # this version. Recorded so it's visible in progress
                # queries rather than silently invisible.
                db.seed_discovery_row(
                    cols["discovery_progress"],
                    company_id,
                    company_name,
                    doc_type,
                    urls[0],
                    status="skipped_multi_url",
                )
                skipped_multi += 1
                continue
            db.seed_discovery_row(
                cols["discovery_progress"], company_id, company_name, doc_type, urls[0]
            )
            seeded += 1
    logger.info(
        f"Discovery seeding: {seeded} work unit(s) ensured, {skipped_multi} multi-url doc type(s) flagged for manual review"
    )


def run_discovery_phase(
    cols: dict,
    budget: TimeBudget,
    company_id: str | None = None,
    limit: int | None = None,
) -> int:
    # limit overrides CFG.DISCOVERY_BATCH_LIMIT for this call (see run_focused)
    overall_limit = CFG.DISCOVERY_BATCH_LIMIT if limit is None else limit
    processed = 0
    while not budget.exhausted():
        remaining = (overall_limit - processed) if overall_limit else CLAIM_CHUNK_SIZE
        if overall_limit and remaining <= 0:
            break
        batch = db.claim_discovery_batch(
            cols["discovery_progress"],
            min(remaining, CLAIM_CHUNK_SIZE),
            dry_run=CFG.DRY_RUN,
            company_id=company_id,
        )
        if not batch:
            break
        attempted = 0
        for row in batch:
            if budget.exhausted():
                break
            try:
                timestamps = cdx.discover(row["url"])
                if CFG.DRY_RUN:
                    logger.info(
                        f"[DRY RUN] Would mark discovery done for {row['url']} ({row['doc_type']}) "
                        f"with {len(timestamps)} snapshot(s) found, and seed that many fetch_progress row(s)"
                    )
                else:
                    db.mark_discovery_done(
                        cols["discovery_progress"], row["_id"], timestamps
                    )
                    for ts in timestamps:
                        db.seed_fetch_row(
                            cols["fetch_progress"],
                            row["company_id"],
                            row["company_name"],
                            row["doc_type"],
                            row["url"],
                            ts,
                        )
                    logger.info(
                        f"Discovered {len(timestamps)} snapshot(s) for {row['url']} ({row['doc_type']})"
                    )
            except cdx.RateLimited as e:
                _halt_on_rate_limit(budget, e)
                break
            except Exception as e:
                logger.error(f"Discovery failed for {row['url']}: {e}")
                if CFG.DRY_RUN:
                    logger.info(
                        f"[DRY RUN] Would mark discovery failed for {row['url']}: {e}"
                    )
                else:
                    db.mark_failed(cols["discovery_progress"], row["_id"], e)
            processed += 1
            attempted += 1
        _release_unattempted(cols["discovery_progress"], batch, attempted)
        if CFG.DRY_RUN:
            # Dry-run rows are never claimed/mutated (see db.claim_discovery_batch),
            # so the exact same batch would come back again next iteration --
            # preview one batch and stop instead of looping over it forever.
            break
    logger.info(f"Discovery phase processed {processed} work unit(s)")
    return processed


def run_fetch_phase(
    cols: dict,
    bucket,
    budget: TimeBudget,
    company_id: str | None = None,
    limit: int | None = None,
) -> tuple[int, set]:
    # limit overrides CFG.FETCH_BATCH_LIMIT for this call (see run_focused)
    overall_limit = CFG.FETCH_BATCH_LIMIT if limit is None else limit
    processed = 0
    affected_company_ids: set = set()
    status_counts: dict = {}

    while not budget.exhausted():
        remaining = (overall_limit - processed) if overall_limit else CLAIM_CHUNK_SIZE
        if overall_limit and remaining <= 0:
            break
        batch = db.claim_fetch_batch(
            cols["fetch_progress"],
            min(remaining, CLAIM_CHUNK_SIZE),
            dry_run=CFG.DRY_RUN,
            company_id=company_id,
        )
        if not batch:
            break
        attempted = 0
        for row in batch:
            if budget.exhausted():
                break
            time.sleep(CFG.FETCH_SLEEP_SECONDS)
            try:
                # fetch.build_and_store_capture already honors CFG.DRY_RUN
                # itself (skips the GCS upload and documents_v2 insert, see
                # fetch.py), so the real network fetch/extract/gate/dedupe
                # steps still run for a realistic preview -- only the Mongo
                # progress-row write below is additionally skipped here.
                status, details = fetch.build_and_store_capture(
                    cols,
                    bucket,
                    row["company_id"],
                    row["company_name"],
                    row["doc_type"],
                    row["url"],
                    row["timestamp"],
                )
                if CFG.DRY_RUN:
                    logger.info(
                        f"[DRY RUN] Would mark fetch_progress '{status}' for {row['url']} @ {row['timestamp']}"
                    )
                else:
                    db.mark_fetch_result(
                        cols["fetch_progress"], row["_id"], status, **details
                    )
                if status == "fetched":
                    affected_company_ids.add(row["company_id"])
                status_counts[status] = status_counts.get(status, 0) + 1
                if status == "failed":
                    # A 429 never gets here (see the RateLimited branch
                    # below), but other transient errors (5xx, timeouts) do --
                    # back off the same as a raised error.
                    time.sleep(CFG.FETCH_BACKOFF_SECONDS)
            except cdx.RateLimited as e:
                _halt_on_rate_limit(budget, e)
                break
            except Exception as e:
                logger.error(f"Fetch failed for {row['url']} @ {row['timestamp']}: {e}")
                if CFG.DRY_RUN:
                    logger.info(
                        f"[DRY RUN] Would mark fetch failed for {row['url']} @ {row['timestamp']}: {e}"
                    )
                else:
                    db.mark_fetch_result(
                        cols["fetch_progress"], row["_id"], "failed", error=str(e)
                    )
                status_counts["failed"] = status_counts.get("failed", 0) + 1
                time.sleep(CFG.FETCH_BACKOFF_SECONDS)
            processed += 1
            attempted += 1
        _release_unattempted(cols["fetch_progress"], batch, attempted)
        if CFG.DRY_RUN:
            # Same reasoning as run_discovery_phase -- nothing was claimed
            # for real, so stop after one preview batch instead of refetching
            # the identical pending rows forever.
            break

    logger.info(f"Fetch phase processed {processed} snapshot(s): {status_counts}")
    return processed, affected_company_ids


def _remaining(limit: int, used: int) -> int | None:
    """What's left of a per-run limit, or None when the limit is 0 (unlimited)."""
    return max(limit - used, 0) if limit else None


def run_focused(cols: dict, bucket, budget: TimeBudget) -> tuple[int, int, set, list]:
    """
    Works one company at a time (see db.pick_next_company for which one),
    up to CFG.COMPANIES_PER_RUN companies, with CFG.DISCOVERY_BATCH_LIMIT /
    CFG.FETCH_BATCH_LIMIT shared across all of them as this run's overall
    task budget. A company only uses up one of the COMPANIES_PER_RUN slots
    if at least one of its snapshots was fetched -- one where the Wayback
    Machine turned out to have nothing (discovery found no snapshots) moves
    straight on to the next company instead of wasting the night.

    Returns (discovery units processed, snapshots processed, affected
    company ids, names of the companies worked on).
    """
    discovered, fetched = 0, 0
    affected_company_ids: set = set()
    tried: set = set()
    worked: list = []

    while not budget.exhausted() and len(worked) < CFG.COMPANIES_PER_RUN:
        fetch_left = _remaining(CFG.FETCH_BATCH_LIMIT, fetched)
        if fetch_left == 0:
            break
        picked = db.pick_next_company(cols, exclude=tried)
        if picked is None:
            break
        company_id, company_name = picked
        tried.add(company_id)
        logger.info(f"Backfilling {company_name} ({company_id}) this run")

        d = run_discovery_phase(
            cols,
            budget,
            company_id=company_id,
            limit=_remaining(CFG.DISCOVERY_BATCH_LIMIT, discovered),
        )
        f, affected = run_fetch_phase(
            cols, bucket, budget, company_id=company_id, limit=fetch_left
        )
        discovered += d
        fetched += f
        affected_company_ids |= affected
        if f:
            worked.append(company_name)
        elif not d:
            # Nothing at all could be done for an eligible company -- the
            # discovery budget is spent (or we were rate limited), so moving
            # on to yet another company would just repeat the same no-op.
            break

    return discovered, fetched, affected_company_ids, worked


def run(cols: dict, bucket) -> None:
    budget = TimeBudget(CFG.TIME_BUDGET_SECONDS)

    seed_discovery_progress(cols)
    if CFG.COMPANIES_PER_RUN:
        discovered, fetched, affected_company_ids, worked = run_focused(
            cols, bucket, budget
        )
    else:
        # COMPANIES_PER_RUN: 0 -- no per-company focus, the batch limits
        # apply across every company at once
        discovered = run_discovery_phase(cols, budget)
        fetched, affected_company_ids = run_fetch_phase(cols, bucket, budget)
        worked = []

    if affected_company_ids and CFG.CREATE_CHANGES and not CFG.DRY_RUN:
        logger.info(
            f"Regenerating .CHANGES.json for {len(affected_company_ids)} affected compan(y/ies)..."
        )
        from source.changes import create_changes_json

        create_changes_json(affected_company_ids)

    summary = db.progress_summary(cols)
    stopped = (
        " (stopped early: rate limited by web.archive.org)"
        if budget.halt_reason == "rate_limited"
        else ""
    )
    logger.info(
        f"Run complete{stopped}. companies={worked} discovery_units_processed={discovered} "
        f"snapshots_processed={fetched}\n{summary}"
    )
    focus = f" ({', '.join(worked)})" if worked else ""
    notify_slack(
        f"Wayback backfill run complete{stopped}{focus}: {discovered} discovery unit(s), {fetched} snapshot(s) processed, "
        f"{len(affected_company_ids)} compan(y/ies) updated.\n"
        f"discovery_progress: {summary.get('discovery_progress')}\n"
        f"fetch_progress: {summary.get('fetch_progress')}"
    )
