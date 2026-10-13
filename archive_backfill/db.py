# Wayback Machine Historical Backfill - Mongo access
# Two databases are involved, on purpose:
#
#   CFG.MAIN_DB_NAME (e.g. transparencydb_dev) -- the LIVE production
#     database. This job only *reads* "companies" from it, and *writes*
#     backfilled snapshots into "documents_v2" using the exact same schema
#     the live crawl pipeline uses, so they show up in the Comparison Tool's
#     diff chain immediately (see fetch.py).
#
#   CFG.BACKFILL_DB_NAME (e.g. archive_backfill) -- a database that belongs
#     entirely to this job. It never holds document content, only two
#     progress-tracking collections that record exactly where each unit of
#     work is in the discover -> fetch -> store chain. This is what makes
#     the job resumable across Cloud Run executions: instead of the original
#     script's local web_archive_links.json/web_archive_failed.json files
#     (lost when the container exits), every step is checkpointed here as it
#     happens.
#
# discovery_progress: one document per (company_id, doc_type, url) -- the
#   CDX timestamp-discovery step for a single source url.
# fetch_progress: one document per (company_id, doc_type, url, timestamp) --
#   the download-and-store step for a single discovered snapshot.

from datetime import datetime, timezone

from pymongo import ASCENDING, MongoClient
from pymongo.server_api import ServerApi

from archive_backfill.settings import CFG, logger

DISCOVERY_STATUSES = (
    "pending",
    "in_progress",
    "done",
    "failed",
    "skipped_multi_url",
    "already_captured",
)
FETCH_STATUSES = (
    "pending",
    "in_progress",
    "fetched",
    "skipped_duplicate",
    "rejected_quality",
    "failed",
    "skipped_precaptured",
)


def connect() -> MongoClient:
    client = MongoClient(
        CFG.MONGODB_URL, server_api=ServerApi("1", strict=True, deprecation_errors=True)
    )
    logger.info("Successfully connected to MongoDB")
    return client


def get_collections(client: MongoClient) -> dict:
    main_db = client[CFG.MAIN_DB_NAME]
    backfill_db = client[CFG.BACKFILL_DB_NAME]
    return {
        "companies": main_db["companies"],
        "documents_v2": main_db["documents_v2"],
        "discovery_progress": backfill_db["discovery_progress"],
        "fetch_progress": backfill_db["fetch_progress"],
    }


def ensure_indexes(cols: dict) -> None:
    cols["discovery_progress"].create_index(
        [("company_id", ASCENDING), ("doc_type", ASCENDING), ("url", ASCENDING)],
        unique=True,
        name="discovery_work_unit",
    )
    cols["discovery_progress"].create_index([("status", ASCENDING)])
    cols["fetch_progress"].create_index(
        [
            ("company_id", ASCENDING),
            ("doc_type", ASCENDING),
            ("url", ASCENDING),
            ("timestamp", ASCENDING),
        ],
        unique=True,
        name="fetch_work_unit",
    )
    cols["fetch_progress"].create_index([("status", ASCENDING)])


def now() -> datetime:
    return datetime.now(timezone.utc)


def seed_discovery_row(
    collection,
    company_id: str,
    company_name: str,
    doc_type: str,
    url: str,
    status: str = "pending",
) -> None:
    """
    Idempotently ensures a discovery_progress row exists for this work unit.
    Never overwrites an existing row's progress -- only inserts if absent.
    """
    collection.update_one(
        {"company_id": company_id, "doc_type": doc_type, "url": url},
        {
            "$setOnInsert": {
                "company_id": company_id,
                "company_name": company_name,
                "doc_type": doc_type,
                "url": url,
                "status": status,
                "attempts": 0,
                "last_attempt": None,
                "last_error": None,
                "timestamps_found": [],
                "created_at": now(),
                "updated_at": now(),
            }
        },
        upsert=True,
    )


def mark_discovery_precaptured(
    collection,
    company_id: str,
    company_name: str,
    doc_type: str,
    url: str,
    note: str = "",
) -> None:
    """
    Marks a (company, doc_type, url) work unit as already covered by prior
    effort outside this job (a manual run, a one-off import, ...), so
    claim_discovery_batch never selects it -- "already_captured" isn't one
    of the statuses its $or query matches (see _claim_batch). Unlike
    seed_discovery_row, this OVERWRITES any existing row (pending, failed,
    even a completed "done"), since marking something precaptured is an
    explicit operator decision that should take precedence over whatever
    state this job had already reached on its own.
    """
    collection.update_one(
        {"company_id": company_id, "doc_type": doc_type, "url": url},
        {
            "$set": {
                "company_id": company_id,
                "company_name": company_name,
                "doc_type": doc_type,
                "url": url,
                "status": "already_captured",
                "last_error": None,
                "precaptured_note": note,
                "updated_at": now(),
            },
            "$setOnInsert": {
                "attempts": 0,
                "last_attempt": None,
                "timestamps_found": [],
                "created_at": now(),
            },
        },
        upsert=True,
    )


def mark_fetch_rows_precaptured(
    collection, company_id: str, doc_type: str | None = None
) -> int:
    """
    Sweeps any non-terminal fetch_progress rows (pending/in_progress/failed)
    for a company -- optionally narrowed to one doc_type -- over to
    "skipped_precaptured". Only relevant if discovery had already run for
    this company before it was marked precaptured, leaving fetch rows
    queued up; a company marked precaptured before its own discovery ever
    ran won't have any fetch_progress rows to begin with. Returns the
    number of rows updated.
    """
    query = {
        "company_id": company_id,
        "status": {"$in": ["pending", "in_progress", "failed"]},
    }
    if doc_type:
        query["doc_type"] = doc_type
    result = collection.update_many(
        query,
        {
            "$set": {
                "status": "skipped_precaptured",
                "last_error": None,
                "updated_at": now(),
            }
        },
    )
    return result.modified_count


def claim_discovery_batch(
    collection, limit: int, dry_run: bool = False, company_id: str | None = None
) -> list:
    """
    Returns up to `limit` discovery_progress rows eligible to run right now
    (pending, or failed-but-past-backoff-and-under-retry-cap), marking each
    as in_progress as it's claimed so a concurrent/overlapping job execution
    doesn't double-process it.

    dry_run=True skips that claiming entirely (a plain read, no status
    mutation) -- see archive_backfill/pipeline.py, which never calls any of
    this module's mark_* functions in dry-run mode either, so nothing about
    a dry run touches Mongo at all. Without this, "claiming" a row (even
    just to preview it) would flip it to in_progress for real, and a dry run
    that never calls mark_discovery_done/mark_failed afterwards would leave
    it stuck there until the in_progress backoff window lapsed.

    company_id, when set, restricts the claim to that one company's rows --
    used by archive_backfill/oneshot.py to backfill a single company without
    touching anyone else's queued work.
    """
    return _claim_batch(collection, limit, dry_run, company_id)


def claim_fetch_batch(
    collection, limit: int, dry_run: bool = False, company_id: str | None = None
) -> list:
    # Oldest snapshot first, so each one is deduped against the one right
    # before it that this same run may have just inserted.
    return _claim_batch(
        collection, limit, dry_run, company_id, sort=[("timestamp", ASCENDING)]
    )


def _claim_batch(
    collection,
    limit: int,
    dry_run: bool = False,
    company_id: str | None = None,
    sort: list | None = None,
) -> list:
    query = _eligible_query(company_id)
    # Real pymongo deserializes a fresh dict per find() call, so returning
    # this directly is safe even though _claim_batch's own find_one_and_update
    # below mutates rows by the same query shape in the non-dry-run path.
    if dry_run:
        return list(collection.find(query, limit=(limit if limit else 0), sort=sort))

    claimed = []
    cursor = collection.find(query, limit=(limit if limit else 0), sort=sort)
    for row in cursor:
        result = collection.find_one_and_update(
            {"_id": row["_id"], "status": row["status"]},
            # Stamping last_attempt at claim time is what lets the
            # in_progress branch in _eligible_query reclaim a row whose
            # worker died before any mark_* call -- a never-attempted row
            # otherwise keeps last_attempt=None, which "$lt" never matches,
            # stranding it.
            {
                "$set": {
                    "status": "in_progress",
                    "last_attempt": now(),
                    "updated_at": now(),
                }
            },
        )
        if result is not None:
            claimed.append(row)
        if limit and len(claimed) >= limit:
            break
    return claimed


# A company counts as "started" once any of its discovery rows has actually
# been attempted (or it has fetch rows) -- skipped_multi_url/already_captured
# rows are set at seeding time without any work happening, so they don't.
STARTED_DISCOVERY_STATUSES = ["in_progress", "done", "failed"]


def pick_next_company(cols: dict, exclude: set | None = None) -> tuple | None:
    """
    Picks the one company this run should work on, so each execution
    focuses on a single platform's history instead of spreading a little
    work across every company at once. Only considers companies with work
    claimable right now (see _eligible_query), excluding `exclude`:

      1. A company already in flight (discovery attempted, or fetch rows
         seeded) -- finish what was started before opening anything new.
      2. Otherwise the next not-yet-started company, alphabetically by name.

    Returns (company_id, company_name), or None when nothing is eligible.
    """
    exclude = exclude or set()
    eligible = _eligible_query()

    names: dict = {}
    for name in ("discovery_progress", "fetch_progress"):
        for row in cols[name].find(eligible):
            if row["company_id"] not in exclude:
                names.setdefault(row["company_id"], row.get("company_name") or "")
    if not names:
        return None

    started = _company_ids(cols["fetch_progress"], {})
    started |= _company_ids(
        cols["discovery_progress"],
        {"status": {"$in": STARTED_DISCOVERY_STATUSES}},
    )
    in_flight = [cid for cid in names if cid in started]
    pool = in_flight or list(names)
    company_id = min(pool, key=lambda cid: (names[cid].lower(), cid))
    return company_id, names[company_id]


def _company_ids(collection, filt: dict) -> set:
    """
    The distinct company_ids matching filt. An aggregation rather than
    collection.distinct(), which connect()'s strict Stable API (v1) rejects.
    """
    pipeline = [{"$match": filt}, {"$group": {"_id": "$company_id"}}]
    return {row["_id"] for row in collection.aggregate(pipeline)}


def _eligible_query(company_id: str | None = None) -> dict:
    """
    Rows eligible to be claimed right now: pending, or failed/in_progress
    but past the retry backoff and under the retry cap.
    """
    from datetime import timedelta

    retry_cutoff = now() - timedelta(minutes=CFG.RETRY_BACKOFF_MINUTES)
    query = {
        "$or": [
            {"status": "pending"},
            {
                "status": "failed",
                "attempts": {"$lt": CFG.MAX_RETRIES},
                "last_attempt": {"$lt": retry_cutoff},
            },
            # A row left "in_progress" by a killed/timed-out execution is
            # eligible again after the same backoff window, capped by
            # MAX_RETRIES just like the "failed" branch above -- otherwise a
            # row that reliably crashes/hangs the worker before any mark_*
            # call runs (so attempts never increments) would be reclaimed
            # and re-crash the worker forever, every backoff window, with no
            # permanent give-up path.
            {
                "status": "in_progress",
                "attempts": {"$lt": CFG.MAX_RETRIES},
                "last_attempt": {"$lt": retry_cutoff},
            },
        ]
    }
    if company_id:
        query["company_id"] = company_id
    return query


def release_claim(collection, row: dict) -> None:
    """
    Undoes a claim on a row that was never actually attempted, restoring
    the pre-claim status/last_attempt from `row` (the snapshot a claim_*
    function returned). attempts is untouched, so a run that stops early
    (rate limited, time budget) doesn't cost the rows it didn't get to.
    """
    collection.update_one(
        {"_id": row["_id"], "status": "in_progress"},
        {
            "$set": {
                "status": row["status"],
                "last_attempt": row.get("last_attempt"),
                "updated_at": now(),
            }
        },
    )


def mark_discovery_done(collection, row_id, timestamps_found: list) -> None:
    collection.update_one(
        {"_id": row_id},
        {
            "$set": {
                "status": "done",
                "timestamps_found": timestamps_found,
                "last_attempt": now(),
                "last_error": None,
                "updated_at": now(),
            },
            "$inc": {"attempts": 1},
        },
    )


def mark_failed(collection, row_id, error: str) -> None:
    collection.update_one(
        {"_id": row_id},
        {
            "$set": {
                "status": "failed",
                "last_attempt": now(),
                "last_error": str(error)[:2000],
                "updated_at": now(),
            },
            "$inc": {"attempts": 1},
        },
    )


def seed_fetch_row(
    collection,
    company_id: str,
    company_name: str,
    doc_type: str,
    url: str,
    timestamp: str,
) -> None:
    collection.update_one(
        {
            "company_id": company_id,
            "doc_type": doc_type,
            "url": url,
            "timestamp": timestamp,
        },
        {
            "$setOnInsert": {
                "company_id": company_id,
                "company_name": company_name,
                "doc_type": doc_type,
                "url": url,
                "timestamp": timestamp,
                "status": "pending",
                "attempts": 0,
                "last_attempt": None,
                "last_error": None,
                "content_type": None,
                "gcs_path": None,
                "documents_v2_id": None,
                "date_fetched": None,
                "created_at": now(),
                "updated_at": now(),
            }
        },
        upsert=True,
    )


def mark_fetch_result(collection, row_id, status: str, **fields) -> None:
    """
    status one of: fetched, skipped_duplicate, rejected_quality, failed.
    Extra keyword args (content_type, gcs_path, documents_v2_id,
    date_fetched, error) are stored alongside when provided.
    """
    update = {
        "status": status,
        "last_attempt": now(),
        "updated_at": now(),
    }
    if "error" in fields:
        update["last_error"] = str(fields["error"])[:2000]
    else:
        update["last_error"] = None
    for key in ("content_type", "gcs_path", "documents_v2_id", "date_fetched"):
        if key in fields:
            update[key] = fields[key]
    collection.update_one({"_id": row_id}, {"$set": update, "$inc": {"attempts": 1}})


def progress_summary(cols: dict) -> dict:
    """Returns a small dict of status -> count for both collections, for logging/Slack."""
    summary = {}
    for name in ("discovery_progress", "fetch_progress"):
        pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        summary[name] = {
            row["_id"]: row["count"] for row in cols[name].aggregate(pipeline)
        }
    return summary
