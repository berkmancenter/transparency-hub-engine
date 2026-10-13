# Tests for archive_backfill/: the Wayback Machine historical backfill job.
# Covers the deterministic/pure logic (CDX timestamp selection, snapshot
# format detection, the fetch quality/dedupe gate) and the progress-tracking
# state transitions in db.py against a lightweight in-memory Mongo double
# (no mongomock dependency in this repo, and no real Mongo server in CI --
# see tests/conftest.py's own no-network philosophy for the rest of the
# suite).

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from archive_backfill import cdx, companies, db, fetch, pipeline
from archive_backfill.pipeline import TimeBudget
from archive_backfill.settings import CFG as BACKFILL_CFG


# --------------------------------------------------------------------------
# cdx.py -- CDX timestamp discovery/selection
# --------------------------------------------------------------------------


class TestIsValidWbTimestamp:
    def test_valid(self):
        assert cdx.is_valid_wb_timestamp("20150401000000") is True

    def test_invalid_format(self):
        assert cdx.is_valid_wb_timestamp("not-a-timestamp") is False

    def test_none(self):
        assert cdx.is_valid_wb_timestamp(None) is False


class TestSelectTimestampForInterval:
    def test_prefers_ok_timestamp_closest_to_midpoint(self):
        ok = [
            cdx.date_from_ts_str("20150402120000"),
            cdx.date_from_ts_str("20150115000000"),
        ]
        result = cdx.select_timestamp_for_interval("example.com", 2015, "A", ok, [])
        assert result == "20150402120000"

    def test_falls_back_to_non_ok_when_no_ok_in_range(self):
        non_ok = [cdx.date_from_ts_str("20151002000000")]
        result = cdx.select_timestamp_for_interval("example.com", 2015, "B", [], non_ok)
        assert result == "20151002000000"

    def test_returns_none_outside_season_window(self):
        # An "A" season timestamp far outside Jan-Jun shouldn't be selected
        ok = [cdx.date_from_ts_str("20151201000000")]
        result = cdx.select_timestamp_for_interval("example.com", 2015, "A", ok, [])
        assert result is None

    def test_returns_none_with_no_candidates(self):
        assert (
            cdx.select_timestamp_for_interval("example.com", 2015, "A", [], []) is None
        )


class TestSelectTimestamps:
    def test_selects_up_to_two_per_year(self):
        ok = ["20150215000000", "20150815000000", "20160301000000"]
        selected = cdx.select_timestamps("example.com", ok, [], 2015, 2016)
        # 2015 A+B, 2016 A only (no candidate near Oct 2016)
        assert len(selected) == 3

    def test_empty_input_returns_empty(self):
        assert cdx.select_timestamps("example.com", [], [], 2015, 2016) == []


class TestLoadCdxPage:
    @patch("archive_backfill.cdx.requests.get")
    def test_raises_on_blocked_site(self, mock_get):
        # Blocked-site detection only kicks in on a non-200 response -- see
        # load_cdx_page's docstring/ordering, ported as-is from the original
        # script. A 200 response is trusted as a successful body regardless
        # of its content.
        mock_get.return_value = MagicMock(status_code=403, text="Blocked Site Error")
        with pytest.raises(cdx.CdxError):
            cdx.load_cdx_page([("url", "example.com")])

    @patch("archive_backfill.cdx.requests.get")
    def test_raises_on_empty_body(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="")
        with pytest.raises(cdx.CdxError):
            cdx.load_cdx_page([("url", "example.com")])

    @patch("archive_backfill.cdx.requests.get")
    def test_returns_body_on_success(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="some cdx line")
        assert cdx.load_cdx_page([("url", "example.com")]) == "some cdx line"


# --------------------------------------------------------------------------
# fetch.py -- snapshot download, text extraction, quality/dedupe gating
# --------------------------------------------------------------------------


class TestFetchSnapshotBytes:
    @patch("archive_backfill.fetch.requests.get")
    def test_detects_pdf_from_content_type(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            content=b"%PDF-1.4",
            headers={"Content-Type": "application/pdf"},
        )
        content, fmt = fetch.fetch_snapshot_bytes(
            "https://example.com/policy.pdf", "20150401000000"
        )
        assert fmt == "pdf"
        assert content == b"%PDF-1.4"

    @patch("archive_backfill.fetch.requests.get")
    def test_detects_html_default(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            content=b"<html></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
        _content, fmt = fetch.fetch_snapshot_bytes(
            "https://example.com/policy", "20150401000000"
        )
        assert fmt == "html"

    @patch("archive_backfill.fetch.requests.get")
    def test_raises_unsupported_format_for_images(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, content=b"\x89PNG", headers={"Content-Type": "image/png"}
        )
        with pytest.raises(fetch.UnsupportedFormat):
            fetch.fetch_snapshot_bytes("https://example.com/logo.png", "20150401000000")

    @patch("archive_backfill.fetch.requests.get")
    def test_raises_fetch_error_on_non_200(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404, content=b"", headers={})
        with pytest.raises(fetch.FetchError):
            fetch.fetch_snapshot_bytes("https://example.com/gone", "20150401000000")

    @patch(
        "archive_backfill.fetch.requests.get",
        side_effect=fetch.requests.RequestException("boom"),
    )
    def test_raises_fetch_error_on_network_failure(self, mock_get):
        with pytest.raises(fetch.FetchError):
            fetch.fetch_snapshot_bytes("https://example.com/policy", "20150401000000")


class TestExtractAndGateText:
    @patch("archive_backfill.fetch.passes_policy_document_check", return_value=True)
    @patch("archive_backfill.fetch.data_quality_check", return_value=True)
    @patch(
        "archive_backfill.fetch.reconstruct_text_from_format",
        return_value="a real privacy policy",
    )
    def test_passes_both_gates(self, mock_reconstruct, mock_quality, mock_policy):
        text = fetch.extract_and_gate_text(
            b"<html>...</html>", "html", "https://example.com/policy"
        )
        assert text == "a real privacy policy"

    @patch("archive_backfill.fetch.data_quality_check", return_value=False)
    @patch("archive_backfill.fetch.reconstruct_text_from_format", return_value="short")
    def test_rejected_by_data_quality_check(self, mock_reconstruct, mock_quality):
        assert (
            fetch.extract_and_gate_text(
                b"<html></html>", "html", "https://example.com/policy"
            )
            is None
        )

    @patch("archive_backfill.fetch.passes_policy_document_check", return_value=False)
    @patch("archive_backfill.fetch.data_quality_check", return_value=True)
    @patch(
        "archive_backfill.fetch.reconstruct_text_from_format",
        return_value="welcome to our homepage",
    )
    def test_rejected_by_policy_classifier(
        self, mock_reconstruct, mock_quality, mock_policy
    ):
        assert (
            fetch.extract_and_gate_text(
                b"<html></html>", "html", "https://example.com/"
            )
            is None
        )

    @patch(
        "archive_backfill.fetch.reconstruct_text_from_format",
        side_effect=ValueError("bad pdf"),
    )
    def test_extraction_failure_raises_text_extraction_error(self, mock_reconstruct):
        # Distinct from a genuine quality-gate rejection (None) -- an
        # extraction exception is transient-looking (e.g. a truncated
        # download), so it must be retryable rather than a permanent
        # "rejected_quality" verdict. See build_and_store_capture, which
        # maps this to status "failed" instead.
        with pytest.raises(fetch.TextExtractionError):
            fetch.extract_and_gate_text(
                b"garbage", "pdf", "https://example.com/policy.pdf"
            )


class TestFindNearestEarlierCapture:
    def test_queries_with_lt_date_and_txt_format_filter(self):
        documents_v2 = MagicMock()
        documents_v2.find.return_value = iter(
            [{"path": "co1/privacy_policy/1700000000"}]
        )
        before = datetime(2015, 1, 1, tzinfo=timezone.utc)

        result = fetch.find_nearest_earlier_capture(
            documents_v2, "co1", "privacy_policy", before
        )

        assert result == {"path": "co1/privacy_policy/1700000000"}
        args, kwargs = documents_v2.find.call_args
        query = args[0]
        assert query["company_id"] == "co1"
        assert query["type"] == "privacy_policy"
        assert query["date_fetched"] == {"$lt": before}
        assert query["formats.txt"] == {"$exists": True}

    def test_returns_none_when_no_prior_capture(self):
        documents_v2 = MagicMock()
        documents_v2.find.return_value = iter([])
        result = fetch.find_nearest_earlier_capture(
            documents_v2, "co1", "privacy_policy", datetime.now(timezone.utc)
        )
        assert result is None


class TestFindNearestLaterCapture:
    def test_queries_with_gt_date_and_ascending_sort(self):
        documents_v2 = MagicMock()
        documents_v2.find.return_value = iter(
            [{"path": "co1/privacy_policy/1800000000"}]
        )
        after = datetime(2015, 1, 1, tzinfo=timezone.utc)

        result = fetch.find_nearest_later_capture(
            documents_v2, "co1", "privacy_policy", after
        )

        assert result == {"path": "co1/privacy_policy/1800000000"}
        args, kwargs = documents_v2.find.call_args
        query = args[0]
        assert query["date_fetched"] == {"$gt": after}
        assert kwargs["sort"] == [("date_fetched", 1)]

    def test_returns_none_when_no_later_capture(self):
        documents_v2 = MagicMock()
        documents_v2.find.return_value = iter([])
        result = fetch.find_nearest_later_capture(
            documents_v2, "co1", "privacy_policy", datetime.now(timezone.utc)
        )
        assert result is None


class TestIsDuplicateOf:
    @patch("source.utils.decode_bytes", return_value="same policy text")
    @patch("archive_backfill.fetch.get_gcs_bytes", return_value=b"same policy text")
    @patch("archive_backfill.fetch.has_content_changed", return_value=False)
    def test_true_when_content_unchanged(self, mock_changed, mock_gcs, mock_decode):
        existing = {"path": "co1/privacy_policy/1700000000"}
        assert fetch.is_duplicate_of("same policy text", existing, bucket=None) is True

    @patch(
        "archive_backfill.fetch.get_gcs_bytes", side_effect=Exception("GCS unavailable")
    )
    def test_fails_open_on_read_error(self, mock_gcs):
        existing = {"path": "co1/privacy_policy/1700000000"}
        # Can't compare -- must NOT report a duplicate, so a real historical
        # snapshot is never silently dropped due to an unrelated I/O error.
        assert fetch.is_duplicate_of("new text", existing, bucket=None) is False


class TestBuildAndStoreCaptureDedup:
    """
    build_and_store_capture checks BOTH the nearest earlier and nearest
    later existing capture before deciding a snapshot is redundant --
    a backfill inserts records out of chronological order, so a duplicate
    can just as easily be adjacent to a LATER existing record as an earlier
    one (see fetch.find_nearest_later_capture).
    """

    @patch("archive_backfill.fetch.write_bytes_to_gcs")
    @patch("archive_backfill.fetch.is_duplicate_of")
    @patch("archive_backfill.fetch.find_nearest_later_capture")
    @patch("archive_backfill.fetch.find_nearest_earlier_capture")
    @patch("archive_backfill.fetch.extract_and_gate_text", return_value="policy text")
    @patch(
        "archive_backfill.fetch.fetch_snapshot_bytes",
        return_value=(b"<html></html>", "html"),
    )
    def test_skips_duplicate_of_later_capture_even_with_no_earlier_one(
        self,
        mock_fetch,
        mock_extract,
        mock_earlier,
        mock_later,
        mock_is_dup,
        mock_write,
    ):
        mock_earlier.return_value = None
        later_doc = {"path": "co1/privacy_policy/wb20200101000000"}
        mock_later.return_value = later_doc
        mock_is_dup.side_effect = lambda text, doc, bucket: doc is later_doc

        cols = {"documents_v2": MagicMock()}
        status, details = fetch.build_and_store_capture(
            cols,
            None,
            "co1",
            "Acme",
            "privacy_policy",
            "https://acme.com/privacy",
            "20150401000000",
        )

        assert status == "skipped_duplicate"
        mock_write.assert_not_called()


# --------------------------------------------------------------------------
# db.py -- progress-tracking state transitions, against an in-memory double
# --------------------------------------------------------------------------


class FakeCollection:
    """
    Minimal stand-in for a pymongo Collection, just enough to exercise
    db.py's upsert/claim/mark helpers without a real MongoDB server. Stores
    documents in an in-memory dict keyed by a synthetic ObjectId-like int.
    """

    def __init__(self):
        self.docs: dict = {}
        self._next_id = 1

    def _match(self, doc, filt):
        for key, value in filt.items():
            if key == "$or":
                if not any(self._match(doc, clause) for clause in value):
                    return False
            elif isinstance(value, dict) and set(value.keys()) & {
                "$lt",
                "$gt",
                "$exists",
                "$in",
            }:
                for op, target in value.items():
                    if op == "$in" and doc.get(key) not in target:
                        return False
                    if op == "$lt" and not (
                        doc.get(key) is not None and doc[key] < target
                    ):
                        return False
                    if op == "$gt" and not (
                        doc.get(key) is not None and doc[key] > target
                    ):
                        return False
            else:
                if doc.get(key) != value:
                    return False
        return True

    def find_one(self, filt):
        for doc in self.docs.values():
            if self._match(doc, filt):
                return doc
        return None

    def find(self, filt=None, sort=None, limit=None):
        # Real pymongo deserializes a fresh dict per document on every find(),
        # so a caller mutating a previously-returned row (e.g. via a later
        # find_one_and_update) never retroactively changes what an earlier
        # find() already handed back. Mirror that here with a shallow copy.
        filt = filt or {}
        results = [dict(doc) for doc in self.docs.values() if self._match(doc, filt)]
        if sort:
            for key, direction in reversed(sort):
                results.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        if limit:
            results = results[:limit]
        return iter(results)

    def update_one(self, filt, update, upsert=False):
        existing = self.find_one(filt)
        if existing is None:
            if not upsert:
                return
            new_doc = dict(filt)
            new_doc["_id"] = self._next_id
            self._next_id += 1
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            self.docs[new_doc["_id"]] = new_doc
            return
        for key, value in update.get("$set", {}).items():
            existing[key] = value
        for key, value in update.get("$inc", {}).items():
            existing[key] = existing.get(key, 0) + value

    def find_one_and_update(self, filt, update):
        existing = self.find_one(filt)
        if existing is None:
            return None
        for key, value in update.get("$set", {}).items():
            existing[key] = value
        for key, value in update.get("$inc", {}).items():
            existing[key] = existing.get(key, 0) + value
        return existing

    # No distinct() on purpose: db.connect() uses the strict Stable API (v1),
    # which rejects the distinct command, so code that calls it must fail here
    # too rather than only against the real server.

    def aggregate(self, pipeline):
        # Supports only the two pipeline shapes db.py uses: db._company_ids'
        # $match + $group-by-company_id, and db.progress_summary's
        # $group-by-status.
        if pipeline[0].get("$match") is not None:
            ids = {doc["company_id"] for doc in self.find(pipeline[0]["$match"])}
            return [{"_id": company_id} for company_id in ids]
        counts: dict = {}
        for doc in self.docs.values():
            counts[doc.get("status")] = counts.get(doc.get("status"), 0) + 1
        return [{"_id": status, "count": count} for status, count in counts.items()]


class TestSeedDiscoveryRow:
    def test_inserts_pending_row(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        assert len(col.docs) == 1
        row = next(iter(col.docs.values()))
        assert row["status"] == "pending"
        assert row["attempts"] == 0

    def test_is_idempotent(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        col.find_one({"company_id": "co1"})["status"] = "done"
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        assert len(col.docs) == 1
        assert next(iter(col.docs.values()))["status"] == "done"


class TestMarkDiscoveryDoneAndFailed:
    def test_mark_done_sets_timestamps_and_increments_attempts(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        row_id = next(iter(col.docs.values()))["_id"]

        db.mark_discovery_done(col, row_id, ["20150401000000", "20151001000000"])

        row = col.docs[row_id]
        assert row["status"] == "done"
        assert row["timestamps_found"] == ["20150401000000", "20151001000000"]
        assert row["attempts"] == 1

    def test_mark_failed_records_error(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        row_id = next(iter(col.docs.values()))["_id"]

        db.mark_failed(col, row_id, Exception("CDX timeout"))

        row = col.docs[row_id]
        assert row["status"] == "failed"
        assert "CDX timeout" in row["last_error"]
        assert row["attempts"] == 1


class TestClaimBatch:
    def test_claims_pending_rows_and_marks_in_progress(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        db.seed_discovery_row(
            col, "co2", "Beta", "terms_of_service", "https://beta.com/tos"
        )

        claimed = db.claim_discovery_batch(col, 10)

        assert len(claimed) == 2
        assert all(
            row["status"] == "pending" for row in claimed
        )  # snapshot returned pre-claim state
        assert all(doc["status"] == "in_progress" for doc in col.docs.values())

    def test_company_id_filter_only_claims_that_company(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        db.seed_discovery_row(
            col, "co2", "Beta", "terms_of_service", "https://beta.com/tos"
        )

        claimed = db.claim_discovery_batch(col, 10, company_id="co2")

        assert [row["company_id"] for row in claimed] == ["co2"]
        statuses = {doc["company_id"]: doc["status"] for doc in col.docs.values()}
        assert statuses == {"co1": "pending", "co2": "in_progress"}

    def test_fetch_batch_claims_oldest_timestamp_first(self):
        col = FakeCollection()
        for ts in ("20200401000000", "20100401000000", "20150401000000"):
            db.seed_fetch_row(
                col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy", ts
            )

        claimed = db.claim_fetch_batch(col, 10)

        assert [row["timestamp"] for row in claimed] == [
            "20100401000000",
            "20150401000000",
            "20200401000000",
        ]

    def test_does_not_reclaim_recently_failed_row_under_backoff(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        row_id = next(iter(col.docs.values()))["_id"]
        db.mark_failed(
            col, row_id, "boom"
        )  # last_attempt = now, still within backoff window

        claimed = db.claim_discovery_batch(col, 10)

        assert claimed == []

    def test_reclaims_failed_row_past_backoff_and_under_retry_cap(self, monkeypatch):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        row_id = next(iter(col.docs.values()))["_id"]
        db.mark_failed(col, row_id, "boom")
        # Push last_attempt back beyond the retry backoff window
        col.docs[row_id]["last_attempt"] = db.now() - timedelta(
            minutes=BACKFILL_CFG.RETRY_BACKOFF_MINUTES + 1
        )

        claimed = db.claim_discovery_batch(col, 10)

        assert len(claimed) == 1

    def test_does_not_reclaim_row_that_exhausted_retries(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        row_id = next(iter(col.docs.values()))["_id"]
        col.docs[row_id]["status"] = "failed"
        col.docs[row_id]["attempts"] = BACKFILL_CFG.MAX_RETRIES
        col.docs[row_id]["last_attempt"] = db.now() - timedelta(days=1)

        claimed = db.claim_discovery_batch(col, 10)

        assert claimed == []

    def test_does_not_reclaim_abandoned_in_progress_row_that_exhausted_retries(self):
        # Regression guard: an in_progress row abandoned by a crash/hang
        # before any mark_* call ever ran (so attempts never incremented)
        # must still eventually stop being reclaimed once its attempts
        # reach MAX_RETRIES, exactly like the "failed" branch above -- a
        # poison-pill snapshot shouldn't be able to crash the worker forever.
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        row_id = next(iter(col.docs.values()))["_id"]
        col.docs[row_id]["status"] = "in_progress"
        col.docs[row_id]["attempts"] = BACKFILL_CFG.MAX_RETRIES
        col.docs[row_id]["last_attempt"] = db.now() - timedelta(days=1)

        claimed = db.claim_discovery_batch(col, 10)

        assert claimed == []

    def test_reclaims_abandoned_in_progress_row_under_retry_cap(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        row_id = next(iter(col.docs.values()))["_id"]
        col.docs[row_id]["status"] = "in_progress"
        col.docs[row_id]["attempts"] = 1
        col.docs[row_id]["last_attempt"] = db.now() - timedelta(
            minutes=BACKFILL_CFG.RETRY_BACKOFF_MINUTES + 1
        )

        claimed = db.claim_discovery_batch(col, 10)

        assert len(claimed) == 1

    def test_claimed_but_never_attempted_row_is_reclaimable_after_backoff(self):
        col = FakeCollection()
        db.seed_fetch_row(
            col,
            "co1",
            "Acme",
            "privacy_policy",
            "https://acme.com/privacy",
            "20150401000000",
        )
        assert len(db.claim_fetch_batch(col, 10)) == 1
        # Worker dies here, before any mark_fetch_result call.
        row = next(iter(col.docs.values()))
        assert row["last_attempt"] is not None
        row["last_attempt"] -= timedelta(minutes=BACKFILL_CFG.RETRY_BACKOFF_MINUTES + 1)

        assert len(db.claim_fetch_batch(col, 10)) == 1

    def test_dry_run_does_not_mutate_status(self):
        col = FakeCollection()
        db.seed_discovery_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )

        claimed = db.claim_discovery_batch(col, 10, dry_run=True)

        assert len(claimed) == 1
        # Nothing was actually claimed -- status is untouched, so a real
        # (non-dry) run later still sees this row as pending.
        assert all(doc["status"] == "pending" for doc in col.docs.values())


class TestMarkFetchResult:
    def test_fetched_records_gcs_path_and_documents_v2_id(self):
        col = FakeCollection()
        db.seed_fetch_row(
            col,
            "co1",
            "Acme",
            "privacy_policy",
            "https://acme.com/privacy",
            "20150401000000",
        )
        row_id = next(iter(col.docs.values()))["_id"]

        db.mark_fetch_result(
            col,
            row_id,
            "fetched",
            content_type="html",
            gcs_path="co1/privacy_policy/wb20150401000000",
            documents_v2_id="abc123",
        )

        row = col.docs[row_id]
        assert row["status"] == "fetched"
        assert row["gcs_path"] == "co1/privacy_policy/wb20150401000000"
        assert row["documents_v2_id"] == "abc123"
        assert row["last_error"] is None

    def test_failed_records_error_and_clears_on_next_success(self):
        col = FakeCollection()
        db.seed_fetch_row(
            col,
            "co1",
            "Acme",
            "privacy_policy",
            "https://acme.com/privacy",
            "20150401000000",
        )
        row_id = next(iter(col.docs.values()))["_id"]

        db.mark_fetch_result(col, row_id, "failed", error="timeout")
        assert col.docs[row_id]["last_error"] == "timeout"

        db.mark_fetch_result(
            col, row_id, "fetched", gcs_path="co1/privacy_policy/wb20150401000000"
        )
        assert col.docs[row_id]["last_error"] is None


# --------------------------------------------------------------------------
# pipeline.py -- TimeBudget
# --------------------------------------------------------------------------


class TestTimeBudget:
    def test_zero_seconds_never_exhausts(self):
        budget = TimeBudget(0)
        assert budget.exhausted() is False

    def test_exhausts_after_elapsed_time(self):
        budget = TimeBudget(0.01)
        import time

        time.sleep(0.02)
        assert budget.exhausted() is True

    def test_not_exhausted_immediately(self):
        budget = TimeBudget(60)
        assert budget.exhausted() is False


# --------------------------------------------------------------------------
# companies.py -- doc_urls shape tolerance
#
# Regression coverage for a real bug found while building mark_precaptured.py:
# the live companies collection turned out to hold doc_urls entries as bare
# url strings (the pre-migration shape), not the {"name": ..., "urls": [...]}
# dicts source/utils.py's format_data() docstring describes -- every one of
# these tests pins down that both shapes work, since which one is actually
# live can change out from under this job.
# --------------------------------------------------------------------------


class TestIterDocTypeUrls:
    def test_old_schema_bare_string(self):
        company = {"doc_urls": {"Privacy Policy": "https://acme.com/privacy"}}
        result = list(companies.iter_doc_type_urls(company))
        assert result == [("privacy_policy", ["https://acme.com/privacy"])]

    def test_new_schema_dict_with_multiple_urls(self):
        company = {
            "doc_urls": {
                "Terms of Service": {
                    "name": "Terms of Service",
                    "urls": ["https://acme.com/tos-1", "https://acme.com/tos-2"],
                }
            }
        }
        result = list(companies.iter_doc_type_urls(company))
        assert result == [
            ("terms_of_service", ["https://acme.com/tos-1", "https://acme.com/tos-2"])
        ]

    def test_mixed_schema_across_doc_types_on_one_company(self):
        company = {
            "doc_urls": {
                "Privacy Policy": "https://acme.com/privacy",
                "Terms of Service": {
                    "name": "Terms of Service",
                    "urls": ["https://acme.com/tos"],
                },
            }
        }
        result = dict(companies.iter_doc_type_urls(company))
        assert result == {
            "privacy_policy": ["https://acme.com/privacy"],
            "terms_of_service": ["https://acme.com/tos"],
        }

    def test_skips_entries_with_no_urls(self):
        company = {"doc_urls": {"Empty": "", "AlsoEmpty": {"urls": []}, "Missing": {}}}
        assert list(companies.iter_doc_type_urls(company)) == []

    def test_missing_doc_urls_yields_nothing(self):
        assert list(companies.iter_doc_type_urls({})) == []

    def test_slugifies_display_names(self):
        company = {"doc_urls": {"Community Guidelines": "https://acme.com/guidelines"}}
        result = list(companies.iter_doc_type_urls(company))
        assert result[0][0] == "community_guidelines"


class TestFindUrlsForDocType:
    def test_finds_match_by_slug(self):
        company = {"doc_urls": {"Privacy Policy": "https://acme.com/privacy"}}
        assert companies.find_urls_for_doc_type(company, "privacy_policy") == [
            "https://acme.com/privacy"
        ]

    def test_returns_empty_list_when_not_found(self):
        company = {"doc_urls": {"Privacy Policy": "https://acme.com/privacy"}}
        assert companies.find_urls_for_doc_type(company, "terms_of_service") == []


# --------------------------------------------------------------------------
# pipeline.py -- DRY_RUN must never write to Mongo
#
# Regression coverage for a real bug: --dry_run previously still let
# claim_*_batch/mark_* write real, permanent status changes (in_progress,
# done, fetched) to Mongo -- only the GCS upload and documents_v2 insert
# were actually skipped. Since _claim_batch never re-selects a row already
# "done"/"fetched", a dry run silently and permanently marked real work as
# complete, losing that historical coverage forever with no error. Every
# test below asserts the opposite: a dry run leaves every touched row
# exactly as it found it.
# --------------------------------------------------------------------------


class TestPipelineDryRunSafety:
    def test_seed_discovery_progress_skips_writes_in_dry_run(self, monkeypatch):
        monkeypatch.setattr(BACKFILL_CFG, "DRY_RUN", True)
        companies_col = FakeCollection()
        companies_col.docs[1] = {
            "_id": "co1",
            "name": "Acme",
            "doc_urls": {"Privacy Policy": "https://acme.com/privacy"},
        }
        discovery_col = FakeCollection()
        cols = {"companies": companies_col, "discovery_progress": discovery_col}

        pipeline.seed_discovery_progress(cols)

        assert discovery_col.docs == {}

    @patch("archive_backfill.pipeline.cdx.discover", return_value=["20150401000000"])
    def test_discovery_phase_does_not_persist_in_dry_run(
        self, mock_discover, monkeypatch
    ):
        monkeypatch.setattr(BACKFILL_CFG, "DRY_RUN", True)
        discovery_col = FakeCollection()
        fetch_col = FakeCollection()
        db.seed_discovery_row(
            discovery_col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy"
        )
        cols = {"discovery_progress": discovery_col, "fetch_progress": fetch_col}

        processed = pipeline.run_discovery_phase(cols, TimeBudget(0))

        assert processed == 1
        # A real run later must still see this row as pending, not
        # permanently "done" from a dry-run preview.
        assert all(doc["status"] == "pending" for doc in discovery_col.docs.values())
        # No fetch_progress rows should have been seeded either.
        assert fetch_col.docs == {}

    @patch("archive_backfill.pipeline.fetch.build_and_store_capture")
    def test_fetch_phase_does_not_persist_in_dry_run(self, mock_build, monkeypatch):
        monkeypatch.setattr(BACKFILL_CFG, "DRY_RUN", True)
        monkeypatch.setattr(BACKFILL_CFG, "FETCH_SLEEP_SECONDS", 0)
        mock_build.return_value = (
            "fetched",
            {"gcs_path": "co1/privacy_policy/wb20150401000000"},
        )
        fetch_col = FakeCollection()
        db.seed_fetch_row(
            fetch_col,
            "co1",
            "Acme",
            "privacy_policy",
            "https://acme.com/privacy",
            "20150401000000",
        )
        cols = {"fetch_progress": fetch_col}

        processed, affected = pipeline.run_fetch_phase(cols, None, TimeBudget(0))

        assert processed == 1
        assert affected == {"co1"}  # still reported for the run summary...
        # ...but nothing was actually written to fetch_progress.
        assert all(doc["status"] == "pending" for doc in fetch_col.docs.values())

    @patch(
        "archive_backfill.pipeline.fetch.build_and_store_capture",
        side_effect=RuntimeError("network blip"),
    )
    def test_fetch_phase_failure_does_not_persist_in_dry_run(
        self, mock_build, monkeypatch
    ):
        monkeypatch.setattr(BACKFILL_CFG, "DRY_RUN", True)
        monkeypatch.setattr(BACKFILL_CFG, "FETCH_SLEEP_SECONDS", 0)
        monkeypatch.setattr(BACKFILL_CFG, "FETCH_BACKOFF_SECONDS", 0)
        fetch_col = FakeCollection()
        db.seed_fetch_row(
            fetch_col,
            "co1",
            "Acme",
            "privacy_policy",
            "https://acme.com/privacy",
            "20150401000000",
        )
        cols = {"fetch_progress": fetch_col}

        pipeline.run_fetch_phase(cols, None, TimeBudget(0))

        assert all(doc["status"] == "pending" for doc in fetch_col.docs.values())


class TestFetchPhaseBackoff:
    @patch("archive_backfill.pipeline.time.sleep")
    @patch(
        "archive_backfill.pipeline.fetch.build_and_store_capture",
        return_value=("failed", {"error": "HTTP 429"}),
    )
    def test_backs_off_after_returned_failure(
        self, mock_build, mock_sleep, monkeypatch
    ):
        # A 429 comes back as a "failed" status, not an exception -- it must
        # still trigger the backoff instead of hammering the next row.
        monkeypatch.setattr(BACKFILL_CFG, "DRY_RUN", False)
        monkeypatch.setattr(BACKFILL_CFG, "FETCH_SLEEP_SECONDS", 1)
        monkeypatch.setattr(BACKFILL_CFG, "FETCH_BACKOFF_SECONDS", 99)
        fetch_col = FakeCollection()
        db.seed_fetch_row(
            fetch_col,
            "co1",
            "Acme",
            "privacy_policy",
            "https://acme.com/privacy",
            "20150401000000",
        )

        pipeline.run_fetch_phase({"fetch_progress": fetch_col}, None, TimeBudget(0))

        assert [c.args[0] for c in mock_sleep.call_args_list] == [1, 99]
        assert all(doc["status"] == "failed" for doc in fetch_col.docs.values())


# --------------------------------------------------------------------------
# Rate limiting -- a 429 ends the run instead of burning retries
# --------------------------------------------------------------------------


class TestRateLimitDetection:
    @patch("archive_backfill.cdx.requests.get")
    def test_cdx_429_raises_rate_limited(self, mock_get):
        mock_get.return_value = MagicMock(status_code=429, text="Too Many Requests")
        with pytest.raises(cdx.RateLimited):
            cdx.load_cdx_page([("url", "example.com")])

    @patch("time.sleep")
    @patch("archive_backfill.cdx.get_timestamps", side_effect=cdx.RateLimited("429"))
    def test_discover_does_not_retry_on_rate_limit(self, mock_ts, mock_sleep):
        with pytest.raises(cdx.RateLimited):
            cdx.discover("https://example.com/privacy")
        assert mock_ts.call_count == 1

    @patch("archive_backfill.fetch.requests.get")
    def test_fetch_429_raises_rate_limited(self, mock_get):
        mock_get.return_value = MagicMock(status_code=429, content=b"", headers={})
        with pytest.raises(cdx.RateLimited):
            fetch.fetch_snapshot_bytes("https://example.com/policy", "20150401000000")


def _seed_fetch_rows(col, timestamps):
    for ts in timestamps:
        db.seed_fetch_row(
            col, "co1", "Acme", "privacy_policy", "https://acme.com/privacy", ts
        )


class TestPipelineHaltsOnRateLimit:
    @patch("archive_backfill.pipeline.time.sleep")
    @patch("archive_backfill.pipeline.fetch.build_and_store_capture")
    def test_fetch_phase_stops_and_releases_unattempted_rows(
        self, mock_build, mock_sleep, monkeypatch
    ):
        monkeypatch.setattr(BACKFILL_CFG, "DRY_RUN", False)
        mock_build.side_effect = [
            ("fetched", {"gcs_path": "co1/privacy_policy/wb20100401000000"}),
            cdx.RateLimited("HTTP 429"),
        ]
        col = FakeCollection()
        _seed_fetch_rows(col, ["20100401000000", "20150401000000", "20200401000000"])
        budget = TimeBudget(0)

        processed, affected = pipeline.run_fetch_phase(
            {"fetch_progress": col}, None, budget
        )

        assert mock_build.call_count == 2  # never reached the third row
        assert processed == 1
        assert affected == {"co1"}
        assert budget.halt_reason == "rate_limited"
        by_ts = {doc["timestamp"]: doc for doc in col.docs.values()}
        assert by_ts["20100401000000"]["status"] == "fetched"
        # The rate-limited row and the one after it go back to pending,
        # without costing an attempt.
        for ts in ("20150401000000", "20200401000000"):
            assert by_ts[ts]["status"] == "pending"
            assert by_ts[ts]["attempts"] == 0
            assert by_ts[ts]["last_attempt"] is None

    @patch("archive_backfill.pipeline.cdx.discover", side_effect=cdx.RateLimited("429"))
    def test_discovery_rate_limit_halts_before_fetch_phase(
        self, mock_discover, monkeypatch
    ):
        monkeypatch.setattr(BACKFILL_CFG, "DRY_RUN", False)
        discovery_col = FakeCollection()
        for url in ("https://acme.com/privacy", "https://acme.com/tos"):
            db.seed_discovery_row(discovery_col, "co1", "Acme", "privacy_policy", url)
        fetch_col = FakeCollection()
        _seed_fetch_rows(fetch_col, ["20150401000000"])
        cols = {"discovery_progress": discovery_col, "fetch_progress": fetch_col}
        budget = TimeBudget(0)

        pipeline.run_discovery_phase(cols, budget)
        with patch("archive_backfill.pipeline.fetch.build_and_store_capture") as b:
            pipeline.run_fetch_phase(cols, None, budget)

        assert mock_discover.call_count == 1
        b.assert_not_called()
        assert all(d["status"] == "pending" for d in discovery_col.docs.values())
        assert all(d["attempts"] == 0 for d in discovery_col.docs.values())
        assert all(d["status"] == "pending" for d in fetch_col.docs.values())

    def test_release_claim_restores_failed_row_state(self):
        col = FakeCollection()
        _seed_fetch_rows(col, ["20150401000000"])
        row = next(iter(col.docs.values()))
        earlier = db.now() - timedelta(hours=2)
        row.update(status="failed", attempts=2, last_attempt=earlier)

        claimed = db.claim_fetch_batch(col, 10)
        db.release_claim(col, claimed[0])

        assert row["status"] == "failed"
        assert row["attempts"] == 2
        assert row["last_attempt"] == earlier


# --------------------------------------------------------------------------
# One company per run -- picked automatically, never spread across all
# --------------------------------------------------------------------------


def _focus_cols():
    return {"discovery_progress": FakeCollection(), "fetch_progress": FakeCollection()}


def _seed_company(cols, company_id, name, doc_types=("privacy_policy",)):
    for doc_type in doc_types:
        db.seed_discovery_row(
            cols["discovery_progress"],
            company_id,
            name,
            doc_type,
            f"https://{company_id}.example/{doc_type}",
        )


class TestPickNextCompany:
    def test_picks_unstarted_companies_alphabetically(self):
        cols = _focus_cols()
        _seed_company(cols, "c2", "zeta")
        _seed_company(cols, "c1", "Alpha")

        assert db.pick_next_company(cols) == ("c1", "Alpha")

    def test_finishes_company_in_flight_before_starting_another(self):
        cols = _focus_cols()
        _seed_company(cols, "c1", "Alpha")
        _seed_company(cols, "c2", "Zeta")
        # Zeta was started on an earlier night and still has snapshots queued
        db.seed_fetch_row(
            cols["fetch_progress"],
            "c2",
            "Zeta",
            "privacy_policy",
            "https://c2.example/privacy_policy",
            "20150401000000",
        )

        assert db.pick_next_company(cols) == ("c2", "Zeta")

    def test_seeding_only_statuses_do_not_count_as_started(self):
        cols = _focus_cols()
        _seed_company(cols, "c1", "Alpha")
        _seed_company(cols, "c2", "Zeta", doc_types=("privacy_policy", "tos"))
        # A multi-url doc type is marked at seeding time, no work happened
        row = cols["discovery_progress"].find_one(
            {"company_id": "c2", "doc_type": "tos"}
        )
        row["status"] = "skipped_multi_url"

        assert db.pick_next_company(cols) == ("c1", "Alpha")

    def test_skips_company_whose_work_is_all_in_retry_backoff(self):
        cols = _focus_cols()
        _seed_company(cols, "c1", "Alpha")
        _seed_company(cols, "c2", "Zeta")
        row = cols["discovery_progress"].find_one({"company_id": "c1"})
        row.update(status="failed", attempts=1, last_attempt=db.now())

        assert db.pick_next_company(cols) == ("c2", "Zeta")

    def test_honors_exclude_and_returns_none_when_nothing_left(self):
        cols = _focus_cols()
        _seed_company(cols, "c1", "Alpha")

        assert db.pick_next_company(cols, exclude={"c1"}) is None
        assert db.pick_next_company(_focus_cols()) is None


class TestRunFocused:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setattr(BACKFILL_CFG, "DRY_RUN", False)
        monkeypatch.setattr(BACKFILL_CFG, "FETCH_SLEEP_SECONDS", 0)
        monkeypatch.setattr(BACKFILL_CFG, "DISCOVERY_BATCH_LIMIT", 10)
        monkeypatch.setattr(BACKFILL_CFG, "FETCH_BATCH_LIMIT", 2)
        monkeypatch.setattr(BACKFILL_CFG, "COMPANIES_PER_RUN", 1)

    @patch(
        "archive_backfill.pipeline.fetch.build_and_store_capture",
        return_value=("fetched", {}),
    )
    @patch(
        "archive_backfill.pipeline.cdx.discover",
        return_value=["20100401000000", "20150401000000", "20200401000000"],
    )
    def test_works_only_one_company_within_the_task_cap(
        self, mock_discover, mock_build
    ):
        cols = _focus_cols()
        _seed_company(cols, "c1", "Alpha", doc_types=("privacy_policy", "tos"))
        _seed_company(cols, "c2", "Zeta")

        discovered, fetched, affected, worked = pipeline.run_focused(
            cols, None, TimeBudget(0)
        )

        assert worked == ["Alpha"]
        assert (discovered, fetched, affected) == (2, 2, {"c1"})
        # Zeta wasn't touched at all
        zeta = cols["discovery_progress"].find_one({"company_id": "c2"})
        assert zeta["status"] == "pending"
        assert {c.args[0] for c in mock_discover.call_args_list} == {
            "https://c1.example/privacy_policy",
            "https://c1.example/tos",
        }

    @patch(
        "archive_backfill.pipeline.fetch.build_and_store_capture",
        return_value=("fetched", {}),
    )
    def test_company_with_no_snapshots_does_not_use_up_the_night(self, mock_build):
        cols = _focus_cols()
        _seed_company(cols, "c1", "Alpha")
        _seed_company(cols, "c2", "Zeta")

        def discover(url):
            return [] if url.startswith("https://c1.") else ["20150401000000"]

        with patch("archive_backfill.pipeline.cdx.discover", side_effect=discover):
            _, fetched, affected, worked = pipeline.run_focused(
                cols, None, TimeBudget(0)
            )

        assert worked == ["Zeta"]
        assert (fetched, affected) == (1, {"c2"})

    @patch("archive_backfill.pipeline.cdx.discover", side_effect=cdx.RateLimited("429"))
    def test_rate_limit_stops_without_moving_on_to_other_companies(self, mock_discover):
        cols = _focus_cols()
        _seed_company(cols, "c1", "Alpha")
        _seed_company(cols, "c2", "Zeta")
        budget = TimeBudget(0)

        _, _, _, worked = pipeline.run_focused(cols, None, budget)

        assert mock_discover.call_count == 1
        assert worked == []
        assert budget.halt_reason == "rate_limited"
