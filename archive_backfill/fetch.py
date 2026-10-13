# Wayback Machine Historical Backfill - snapshot fetch, quality gate, and storage
#
# For a single discovered (company, doc_type, url, timestamp), this module:
#   1. Downloads the raw archived snapshot bytes from web.archive.org
#   2. Extracts text from it (reusing source/async_compare.py's own
#      html/pdf-to-text reconstruction, so a backfilled document's .txt is
#      produced the exact same way a live-pipeline reconstruction would be)
#   3. Runs the SAME quality gates the rest of the pipeline uses before
#      trusting a capture -- source/async_compare.py's data_quality_check
#      and source/policy_classifier.py's is_policy_document (via
#      passes_policy_document_check) -- so this new ingestion path can't
#      reintroduce the kind of bad captures that were previously cleaned up
#      by hand (homepages, cookie walls, 404s, login walls; see
#      scripts/find_bad_captures.py).
#   4. Skips it if it's a near-duplicate of the nearest EARLIER existing
#      documents_v2 capture for this company/doc_type (fuzzy match via
#      source/async_compare.py's has_content_changed), the same redundancy
#      check the live pipeline itself uses -- otherwise a company whose
#      policy didn't change for a decade would get a dozen near-identical
#      historical snapshots.
#   5. Uploads the raw format + reconstructed .txt to GCS and inserts a
#      documents_v2 record, using the exact schema the live pipeline writes
#      (see source/process.py ProcessCrawl), plus a couple of additive,
#      backfill-only fields (source, wayback_url) that don't affect any
#      existing reader of the collection.

import os
import sys
from datetime import timezone
from pathlib import Path

import requests

from archive_backfill import cdx
from archive_backfill.settings import CFG, logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source.async_compare import (  # noqa: E402
    data_quality_check,
    has_content_changed,
    passes_policy_document_check,
    reconstruct_text_from_format,
)
from source.globals import CFG as ROOT_CFG  # noqa: E402
from source.utils import get_gcs_bytes  # noqa: E402


class FetchError(Exception):
    """Raised when the snapshot itself couldn't be downloaded."""


class UnsupportedFormat(Exception):
    """Raised when the snapshot's content-type isn't one this job can process (html/pdf)."""


class TextExtractionError(Exception):
    """
    Raised when reconstructing text from a successfully-downloaded capture
    fails (e.g. a truncated download or malformed PDF triggering an
    exception inside reconstruct_text_from_format) -- distinct from a clean
    extraction that then genuinely fails a quality gate. This is a
    transient-looking failure, not a verdict on the document's content, so
    build_and_store_capture maps it to a retryable "failed" status instead
    of "rejected_quality" (terminal, never retried -- see
    archive_backfill/db.py's _claim_batch).
    """


def snapshot_url(url: str, timestamp: str) -> str:
    # The "id_" modifier returns the raw, unmodified capture -- no Wayback
    # toolbar or link-rewriting injected into the HTML -- which is what we
    # want both for faithful text extraction and for byte-for-byte parity
    # with what a browser would have seen at that url at that time.
    return f"https://web.archive.org/web/{timestamp}id_/{url}"


def fetch_snapshot_bytes(url: str, timestamp: str) -> tuple[bytes, str]:
    """
    Downloads the raw snapshot. Returns (content_bytes, detected_format)
    where detected_format is "html" or "pdf".

    Raises FetchError on a network/HTTP failure, UnsupportedFormat if the
    content-type isn't one this job knows how to turn into text.
    """
    wb_url = snapshot_url(url, timestamp)
    try:
        resp = requests.get(wb_url, headers=cdx.HTTP_HEADERS, timeout=60)
    except requests.RequestException as e:
        raise FetchError(f"Request failed for {wb_url}: {e}")

    if resp.status_code == 429:
        raise cdx.RateLimited(f"HTTP 429 for {wb_url}")
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code} for {wb_url}")

    content_type = resp.headers.get("Content-Type", "").lower()
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return resp.content, "pdf"
    if "html" in content_type or content_type == "" or content_type.startswith("text/"):
        return resp.content, "html"
    raise UnsupportedFormat(f"Unsupported content-type '{content_type}' for {wb_url}")


def extract_and_gate_text(raw_bytes: bytes, fmt: str, url: str) -> str | None:
    """
    Reconstructs text from the raw capture and runs it through the same
    quality gates the live pipeline applies. Returns the text if it passes,
    None if it was successfully extracted but genuinely fails a quality gate.

    Raises TextExtractionError if reconstruction itself raised -- see that
    class's docstring for why this is kept distinct from a None return.
    """
    try:
        text = reconstruct_text_from_format(raw_bytes, fmt, fmt, [url])
    except Exception as e:
        raise TextExtractionError(
            f"Text extraction failed for {url} ({fmt}): {e}"
        ) from e

    if not data_quality_check(text):
        logger.info(f"Rejected {url}: failed data_quality_check")
        return None
    if not passes_policy_document_check(text):
        logger.info(f"Rejected {url}: policy-document classifier rejected it")
        return None
    return text


def find_nearest_earlier_capture(
    documents_v2, company_id: str, doc_type: str, before_date
):
    """
    Returns the most recent existing documents_v2 record for this
    company/doc_type strictly before `before_date` that has a .txt format
    (needed to fuzzy-compare against), or None if there isn't one -- mirrors
    source/process.py ProcessCrawl.verify_content's own prior-capture
    lookup, but anchored on chronological position rather than "most
    recently written to Mongo", since a backfill inserts records out of
    wall-clock order relative to the live pipeline's own writes.
    """
    cursor = documents_v2.find(
        {
            "company_id": company_id,
            "type": doc_type,
            "date_fetched": {"$lt": before_date},
            "formats.txt": {"$exists": True},
        },
        sort=[("date_fetched", -1)],
        limit=1,
    )
    return next(cursor, None)


def find_nearest_later_capture(
    documents_v2, company_id: str, doc_type: str, after_date
):
    """
    The mirror image of find_nearest_earlier_capture: the earliest existing
    documents_v2 record for this company/doc_type strictly after
    `after_date`, or None. A backfill can insert a snapshot anywhere in an
    existing timeline (not just before the earliest live capture), so a
    redundant snapshot can just as easily be a near-duplicate of the record
    that comes right AFTER it as the one right before -- checking only the
    earlier side would insert a spurious extra version in exactly that case.
    """
    cursor = documents_v2.find(
        {
            "company_id": company_id,
            "type": doc_type,
            "date_fetched": {"$gt": after_date},
            "formats.txt": {"$exists": True},
        },
        sort=[("date_fetched", 1)],
        limit=1,
    )
    return next(cursor, None)


def is_duplicate_of(new_text: str, existing_doc: dict, bucket) -> bool:
    """
    True if new_text is NOT meaningfully different from existing_doc's own
    .txt capture (i.e. this snapshot would be redundant to keep).
    """
    txt_path = f"{existing_doc['path']}.txt"
    try:
        old_bytes = get_gcs_bytes(txt_path, bucket)
    except Exception as e:
        # Can't read the comparison target -- fail open (treat as not a
        # duplicate) rather than silently dropping a real historical
        # snapshot because of an unrelated GCS/FUSE hiccup.
        logger.warning(
            f"Could not read {txt_path} for dedupe comparison, keeping snapshot: {e}"
        )
        return False
    from source.utils import decode_bytes

    old_text = decode_bytes(old_bytes)
    return not has_content_changed(old_text, new_text)


def _local_captures_path(gcp_path: str) -> Path:
    return ROOT_CFG.CAPTURES_DIR / gcp_path


def write_bytes_to_gcs(gcp_path: str, content: bytes, bucket) -> None:
    """
    Writes `content` to `gcp_path` in the document store, using whichever of
    the two upload strategies the live pipeline itself uses (see
    source/pipeline.py and source/utils.py upload_to_GCP):

      - IN_CLOUD: the bucket is mounted as a GCS FUSE volume at
        ROOT_CFG.CAPTURES_DIR, so writing there IS writing to GCS.
      - otherwise (local dev / Docker against a real bucket): the GCS API
        directly, via the same Bucket handle source/pipeline.py builds.
      - USE_GCP disabled entirely: falls back to the same local path as the
        FUSE case, purely so local testing has somewhere to inspect output.

    No-ops (after logging) when CFG.DRY_RUN is set.
    """
    if CFG.DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(content)} bytes to {gcp_path}")
        return

    if ROOT_CFG.IN_CLOUD or not ROOT_CFG.USE_GCP:
        local_path = _local_captures_path(gcp_path)
        os.makedirs(local_path.parent, exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(content)
        return

    blob = bucket.blob(gcp_path)
    blob.upload_from_string(content)


def build_and_store_capture(
    cols: dict,
    bucket,
    company_id: str,
    company_name: str,
    doc_type: str,
    url: str,
    timestamp: str,
) -> tuple[str, dict]:
    """
    Runs the full fetch -> extract -> gate -> dedupe -> store sequence for
    one snapshot.

    Returns (status, details) where status is one of "fetched",
    "rejected_quality", "skipped_duplicate", "failed", and details holds
    whatever fields are relevant to record on the fetch_progress row (see
    archive_backfill/db.py mark_fetch_result).
    """
    try:
        raw_bytes, fmt = fetch_snapshot_bytes(url, timestamp)
    except UnsupportedFormat as e:
        return "rejected_quality", {"error": str(e)}
    except FetchError as e:
        return "failed", {"error": str(e)}

    try:
        text = extract_and_gate_text(raw_bytes, fmt, url)
    except TextExtractionError as e:
        return "failed", {"error": str(e)}
    if text is None:
        return "rejected_quality", {"content_type": fmt}

    snapshot_date = cdx.date_from_ts_str(timestamp).replace(tzinfo=timezone.utc)
    nearest_earlier = find_nearest_earlier_capture(
        cols["documents_v2"], company_id, doc_type, snapshot_date
    )
    nearest_later = find_nearest_later_capture(
        cols["documents_v2"], company_id, doc_type, snapshot_date
    )
    if (
        nearest_earlier is not None and is_duplicate_of(text, nearest_earlier, bucket)
    ) or (nearest_later is not None and is_duplicate_of(text, nearest_later, bucket)):
        return "skipped_duplicate", {"content_type": fmt}

    path = f"{company_id}/{doc_type}/wb{timestamp}"
    if CFG.DRY_RUN:
        logger.info(f"[DRY RUN] Would store new capture at {path} ({fmt} + txt)")
        return "fetched", {
            "content_type": fmt,
            "gcs_path": path,
            "date_fetched": snapshot_date,
        }

    write_bytes_to_gcs(f"{path}.{fmt}", raw_bytes, bucket)
    write_bytes_to_gcs(f"{path}.txt", text.encode("utf-8"), bucket)

    formats = {
        fmt: {"extension": fmt, "original_url": [url], "wayback_timestamp": timestamp},
        "txt": {
            "extension": "txt",
            "original_url": [url],
            "wayback_timestamp": timestamp,
        },
    }
    record = {
        "company_id": company_id,
        "path": path,
        "type": doc_type,
        "date_fetched": snapshot_date,
        "public_url": f"https://storage.googleapis.com/{ROOT_CFG.GCP_BUCKET}/{path}",
        "formats": formats,
        # Additive fields, not read by the live pipeline or Comparison Tool,
        # kept purely for traceability/auditing of backfilled history.
        "source": "wayback_backfill",
        "wayback_url": f"https://web.archive.org/web/{timestamp}/{url}",
        "company_name": company_name,
    }
    result = cols["documents_v2"].insert_one(record)
    logger.info(f"Inserted backfilled capture {path} for {company_name} ({doc_type})")
    return "fetched", {
        "content_type": fmt,
        "gcs_path": path,
        "documents_v2_id": result.inserted_id,
        "date_fetched": snapshot_date,
    }
