# Tests for source/changes.py: the patience-diff engine and the
# create_changes_json() pipeline that turns pairs of stored captures into
# .CHANGES.json documents for the Transparency Hub's comparison tool.

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from bson import ObjectId

from conftest import FakeCollection, FakeMongoClient
from source.globals import CFG
from source import changes as changes_module


def make_doc(_id, path, date, company_id="co1", doc_type="privacy_policy", fmt="txt"):
    """`path` should be the extension-less base path stored on a document
    (e.g. "co1/privacy_policy/1") -- create_changes_json appends ".txt"
    itself when it needs the txt capture's file path."""
    return {
        "_id": _id,
        "company_id": company_id,
        "type": doc_type,
        "formats": {fmt: {"extension": fmt, "original_url": "https://example.com"}},
        "path": path,
        "date_fetched": date,
    }


# --------------------------------------------------------------------------
# tokenize / separate_other_chars
# --------------------------------------------------------------------------


class TestTokenize:
    def test_empty_string(self):
        assert changes_module.tokenize("") == []

    def test_splits_words_whitespace_and_punctuation(self):
        assert changes_module.tokenize("Hi there!") == ["Hi", " ", "there", "!"]


class TestSeparateOtherChars:
    def test_splits_words_and_tracks_positions(self):
        tokens = changes_module.tokenize("Hello, world!")
        words, other_chars = changes_module.separate_other_chars(tokens)

        assert words == ["Hello", "world"]
        assert other_chars[1] == ", "
        assert other_chars[2] == "!"

    def test_trailing_characters_captured(self):
        tokens = changes_module.tokenize("end.")
        words, other_chars = changes_module.separate_other_chars(tokens)

        assert words == ["end"]
        assert other_chars[1] == "."


# --------------------------------------------------------------------------
# patience_diff_tokens / char_diff
# --------------------------------------------------------------------------


class TestPatienceDiffTokens:
    def test_reconstructs_both_token_lists(self):
        old = ["the", "quick", "brown", "fox"]
        new = ["the", "slow", "brown", "fox", "jumps"]

        diff = changes_module.patience_diff_tokens(old, new)

        assert [t for t, added, _removed in diff if not added] == old
        assert [t for t, _added, removed in diff if not removed] == new

    def test_no_common_tokens_all_removed_then_added(self):
        old = ["alpha", "beta"]
        new = ["gamma", "delta"]

        diff = changes_module.patience_diff_tokens(old, new)

        assert [t for t, _added, removed in diff if removed] == old
        assert [t for t, added, _removed in diff if added] == new


class TestCharDiff:
    def test_reconstructs_old_and_new_strings(self):
        old, new = "kitten", "sitting"

        diff = changes_module.char_diff(old, new)

        assert "".join(c for c, added, _removed in diff if not added) == old
        assert "".join(c for c, _added, removed in diff if not removed) == new


# --------------------------------------------------------------------------
# reconstruct_with_whitespace / collate_diff
# --------------------------------------------------------------------------


class TestReconstructWithWhitespace:
    def test_rebuilds_unchanged_text_with_whitespace(self):
        old_text = "hello world"
        tokens = changes_module.tokenize(old_text)
        words, sp = changes_module.separate_other_chars(tokens)
        word_diff = [(w, False, False) for w in words]

        result = changes_module.reconstruct_with_whitespace(word_diff, sp, sp)

        assert "".join(t for t, _a, _r in result) == old_text


class TestCollateDiff:
    def test_empty_input(self):
        assert changes_module.collate_diff([]) == []

    def test_merges_adjacent_same_flag_tokens(self):
        diff_result = [("a", False, False), ("b", False, False), ("c", True, False)]

        assert changes_module.collate_diff(diff_result) == [
            ("ab", False, False),
            ("c", True, False),
        ]


# --------------------------------------------------------------------------
# create_patience_diff_data
# --------------------------------------------------------------------------


class TestCreatePatienceDiffData:
    @pytest.mark.parametrize(
        "old,new",
        [
            ("The quick brown fox.", "The slow brown fox."),
            ("Hello world", "Hello there, world"),
            ("Same text unchanged", "Same text unchanged"),
            ("", "Brand new content appeared"),
            ("Content that will vanish", ""),
        ],
    )
    def test_reconstructs_old_and_new_text(self, old, new):
        diff_data = changes_module.create_patience_diff_data(old, new)

        reconstructed_old = "".join(t for t, added, _removed in diff_data if not added)
        reconstructed_new = "".join(
            t for t, _added, removed in diff_data if not removed
        )

        assert reconstructed_old == old
        assert reconstructed_new == new

    def test_old_side_whitespace_can_be_lost_at_replacement_boundaries(self):
        """reconstruct_with_whitespace prefers the *new* document's
        whitespace for an unchanged word (its own docstring: "prefer new
        special characters, fall back to old"). When that unchanged word
        immediately follows a run of added tokens, the connecting separator
        is stamped with the added run's flag, so it's excluded from the
        old-side reconstruction even though the old document did have a
        separator there. This is a real gap in old-text round-tripping --
        acceptable for a highlighted diff view, but not exact reconstruction
        -- and this test documents it rather than asserting the (incorrect)
        universal round-trip invariant."""
        old = "Remove this sentence entirely."
        new = "Totally new sentence instead."

        diff_data = changes_module.create_patience_diff_data(old, new)

        reconstructed_old = "".join(t for t, added, _removed in diff_data if not added)
        reconstructed_new = "".join(
            t for t, _added, removed in diff_data if not removed
        )

        assert reconstructed_new == new
        assert reconstructed_old == "Remove thissentence entirely "
        assert reconstructed_old != old


# --------------------------------------------------------------------------
# analyze_topic_changes
# --------------------------------------------------------------------------


class TestAnalyzeTopicChanges:
    def test_detects_added_and_removed_topic_matches(self):
        CFG.TOPIC_PATTERNS = {"Cookies": [re.compile(r"\bcookies?\b", re.IGNORECASE)]}

        result = changes_module.analyze_topic_changes(
            "We now use Cookies.", "We used to avoid cookies."
        )

        assert "Cookies" in result
        added_match, removed_match, _patterns = result["Cookies"]
        assert added_match is True
        assert removed_match is True

    def test_no_match_returns_empty_dict(self):
        CFG.TOPIC_PATTERNS = {"Cookies": [re.compile(r"\bcookies?\b", re.IGNORECASE)]}

        result = changes_module.analyze_topic_changes(
            "Nothing relevant here", "Also nothing here"
        )

        assert result == {}


# --------------------------------------------------------------------------
# _fetch_html_trimmed / get_diff_text
# --------------------------------------------------------------------------


class TestFetchHtmlTrimmed:
    def test_returns_none_when_capture_has_no_html_format(self):
        cache = changes_module.DocumentCache(capacity=2)
        capture = {"path": "co1/privacy_policy/1", "formats": {"txt": {"extension": "txt"}}}

        assert changes_module._fetch_html_trimmed(capture, None, cache) is None

    def test_fetches_and_trims_html_then_caches_it(self, monkeypatch):
        cache = changes_module.DocumentCache(capacity=2)
        capture = {"path": "co1/privacy_policy/1", "formats": {"html": {"extension": "html"}}}
        fetch_calls = []
        monkeypatch.setattr(
            changes_module,
            "get_gcs_bytes",
            lambda path, bucket: (fetch_calls.append(path), b"<p>raw html</p>")[1],
        )
        import source.boundary_classifier as bc

        monkeypatch.setattr(
            bc, "extract_policy_text", lambda html, threshold=None, model_path=None: "trimmed text"
        )

        result = changes_module._fetch_html_trimmed(capture, None, cache)

        assert result == "trimmed text"
        assert fetch_calls == ["co1/privacy_policy/1.html"]
        # Second call should hit the cache, not fetch again.
        changes_module._fetch_html_trimmed(capture, None, cache)
        assert fetch_calls == ["co1/privacy_policy/1.html"]

    def test_returns_none_and_logs_when_fetch_fails(self, monkeypatch, caplog):
        cache = changes_module.DocumentCache(capacity=2)
        capture = {"path": "co1/privacy_policy/1", "formats": {"html": {"extension": "html"}}}
        monkeypatch.setattr(
            changes_module,
            "get_gcs_bytes",
            lambda path, bucket: (_ for _ in ()).throw(TimeoutError("slow")),
        )

        with caplog.at_level(logging.WARNING):
            result = changes_module._fetch_html_trimmed(capture, None, cache)

        assert result is None
        assert "using untrimmed text instead" in caplog.text

    def test_returns_none_instead_of_crashing_on_malformed_html_format_metadata(
        self, monkeypatch, caplog
    ):
        # Regression: a formats.html entry missing "extension" (legacy/
        # malformed data) must fail open like any other failure here, not
        # raise KeyError out of the function and crash the whole run.
        cache = changes_module.DocumentCache(capacity=2)
        capture = {"path": "co1/privacy_policy/1", "formats": {"html": {}}}

        with caplog.at_level(logging.WARNING):
            result = changes_module._fetch_html_trimmed(capture, None, cache)

        assert result is None
        assert "using untrimmed text instead" in caplog.text

    def test_returns_none_when_extraction_yields_no_text(self, monkeypatch):
        # Regression: extract_policy_text can legitimately return "" (no
        # blocks found at all). An empty string is not None, so treating it
        # as a successful trim would produce a nonsensical empty diff
        # instead of falling back to the real .txt text.
        cache = changes_module.DocumentCache(capacity=2)
        capture = {"path": "co1/privacy_policy/1", "formats": {"html": {"extension": "html"}}}
        monkeypatch.setattr(changes_module, "get_gcs_bytes", lambda path, bucket: b"<html></html>")
        import source.boundary_classifier as bc

        monkeypatch.setattr(
            bc, "extract_policy_text", lambda html, threshold=None, model_path=None: ""
        )

        result = changes_module._fetch_html_trimmed(capture, None, cache)

        assert result is None
        assert cache.get("co1/privacy_policy/1.html") is None


class TestGetDiffText:
    def _caches(self):
        return changes_module.DocumentCache(capacity=4), changes_module.DocumentCache(capacity=4)

    def test_uses_trimmed_html_when_enabled_and_both_sides_have_it(self, monkeypatch):
        CFG.USE_DIFF_TRIMMER = True
        older = {"path": "co1/privacy_policy/1", "formats": {"html": {"extension": "html"}}}
        newer = {"path": "co1/privacy_policy/2", "formats": {"html": {"extension": "html"}}}
        monkeypatch.setattr(
            changes_module,
            "_fetch_html_trimmed",
            lambda capture, bucket, cache: f"trimmed-{capture['path']}",
        )
        doc_cache, html_cache = self._caches()

        older_text, newer_text = changes_module.get_diff_text(
            older, newer, None, doc_cache, html_cache
        )

        assert (older_text, newer_text) == ("trimmed-co1/privacy_policy/1", "trimmed-co1/privacy_policy/2")

    def test_falls_back_to_txt_when_trimmer_disabled(self, monkeypatch):
        CFG.USE_DIFF_TRIMMER = False
        older = {"path": "co1/privacy_policy/1", "formats": {"html": {"extension": "html"}}}
        newer = {"path": "co1/privacy_policy/2", "formats": {"html": {"extension": "html"}}}
        texts = {
            "co1/privacy_policy/1.txt": "older raw text",
            "co1/privacy_policy/2.txt": "newer raw text",
        }
        monkeypatch.setattr(
            changes_module, "get_gcs_bytes", lambda path, bucket: texts[path].encode()
        )
        doc_cache, html_cache = self._caches()

        older_text, newer_text = changes_module.get_diff_text(
            older, newer, None, doc_cache, html_cache
        )

        assert (older_text, newer_text) == ("older raw text", "newer raw text")

    def test_falls_back_to_txt_for_both_sides_when_only_one_has_html(self, monkeypatch):
        # Regression guard: trimming exactly one side would diff trimmed
        # text against untrimmed text, showing extraction-style noise as if
        # it were a real content change. Both sides must fall back together.
        CFG.USE_DIFF_TRIMMER = True
        older = {"path": "co1/privacy_policy/1", "formats": {"html": {"extension": "html"}}}
        newer = {"path": "co1/privacy_policy/2", "formats": {"txt": {"extension": "txt"}}}
        texts = {
            "co1/privacy_policy/1.txt": "older raw text",
            "co1/privacy_policy/2.txt": "newer raw text",
        }
        monkeypatch.setattr(
            changes_module, "get_gcs_bytes", lambda path, bucket: texts[path].encode()
        )
        doc_cache, html_cache = self._caches()

        older_text, newer_text = changes_module.get_diff_text(
            older, newer, None, doc_cache, html_cache
        )

        assert (older_text, newer_text) == ("older raw text", "newer raw text")

    def test_falls_back_to_txt_for_both_sides_when_trim_fails(self, monkeypatch):
        CFG.USE_DIFF_TRIMMER = True
        older = {"path": "co1/privacy_policy/1", "formats": {"html": {"extension": "html"}}}
        newer = {"path": "co1/privacy_policy/2", "formats": {"html": {"extension": "html"}}}
        texts = {
            "co1/privacy_policy/1.txt": "older raw text",
            "co1/privacy_policy/2.txt": "newer raw text",
        }
        monkeypatch.setattr(changes_module, "_fetch_html_trimmed", lambda *a, **k: None)
        monkeypatch.setattr(
            changes_module, "get_gcs_bytes", lambda path, bucket: texts[path].encode()
        )
        doc_cache, html_cache = self._caches()

        older_text, newer_text = changes_module.get_diff_text(
            older, newer, None, doc_cache, html_cache
        )

        assert (older_text, newer_text) == ("older raw text", "newer raw text")


# --------------------------------------------------------------------------
# create_changes_json -- local (non-Mongo) backend
# --------------------------------------------------------------------------


class TestCreateChangesJsonLocal:
    def _setup(self, monkeypatch, tmp_path, documents, changes_db=None):
        CFG.USE_MONGO = False
        CFG.IN_CLOUD = False
        CFG.USE_GCP = False
        CFG.CAPTURES_DIR = tmp_path
        CFG.CHANGES_DIR = Path("changes")
        CFG.DOCUMENTS_BSON = Path("db/documents.bson")
        CFG.CHANGES_BSON = Path("db/changes.bson")

        def fake_pull_db(path):
            if path == CFG.DOCUMENTS_BSON:
                return documents
            if path == CFG.CHANGES_BSON:
                return changes_db if changes_db is not None else []
            raise AssertionError(f"unexpected pull_db path {path}")

        monkeypatch.setattr(changes_module, "pull_db", fake_pull_db)

    def test_generates_all_pairwise_combinations(self, monkeypatch, tmp_path):
        texts = {
            "co1/privacy_policy/1.txt": "Version one text content here",
            "co1/privacy_policy/2.txt": "Version two text content here now",
            "co1/privacy_policy/3.txt": "Version three text content appears",
        }
        documents = [
            make_doc(
                ObjectId(),
                "co1/privacy_policy/1",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
            make_doc(
                ObjectId(),
                "co1/privacy_policy/2",
                datetime(2024, 2, 1, tzinfo=timezone.utc),
            ),
            make_doc(
                ObjectId(),
                "co1/privacy_policy/3",
                datetime(2024, 3, 1, tzinfo=timezone.utc),
            ),
        ]
        self._setup(monkeypatch, tmp_path, documents)
        monkeypatch.setattr(
            changes_module, "get_gcs_bytes", lambda path, bucket: texts[path].encode()
        )
        saved = []
        monkeypatch.setattr(
            changes_module, "save_db", lambda docs, path: saved.extend(docs)
        )

        changes_module.create_changes_json()

        # 3 valid captures -> every (i, j) pair with i < j: (0,1) (0,2) (1,2)
        assert len(saved) == 3
        changes_files = list(
            (tmp_path / "changes" / "co1" / "privacy_policy").glob("*.CHANGES.json")
        )
        assert len(changes_files) == 3

    def test_diff_trimmer_uses_trimmed_html_but_keeps_txt_path_metadata(
        self, monkeypatch, tmp_path
    ):
        CFG.USE_DIFF_TRIMMER = True

        def doc_with_html(_id, path, date):
            # A document needs a "txt" format to enter the pipeline at all
            # (the $match/filter both backends apply); "html" is the extra
            # format that makes it eligible for diff trimming.
            doc = make_doc(_id, path, date)
            doc["formats"]["html"] = {"extension": "html", "original_url": "https://example.com"}
            return doc

        documents = [
            doc_with_html(ObjectId(), "co1/privacy_policy/1", datetime(2024, 1, 1, tzinfo=timezone.utc)),
            doc_with_html(ObjectId(), "co1/privacy_policy/2", datetime(2024, 2, 1, tzinfo=timezone.utc)),
        ]
        self._setup(monkeypatch, tmp_path, documents)
        monkeypatch.setattr(
            changes_module,
            "get_gcs_bytes",
            lambda path, bucket: b"<nav>chrome</nav><p>real policy text</p>",
        )
        import source.boundary_classifier as bc

        monkeypatch.setattr(
            bc,
            "extract_policy_text",
            lambda html, threshold=None, model_path=None: "TRIMMED VERSION",
        )
        monkeypatch.setattr(changes_module, "save_db", lambda docs, path: None)

        changes_module.create_changes_json()

        changes_files = list(
            (tmp_path / "changes" / "co1" / "privacy_policy").glob("*.CHANGES.json")
        )
        assert len(changes_files) == 1
        data = json.loads(changes_files[0].read_text(encoding="utf-8"))

        # The diff is built from the trimmed text, not the raw html.
        diff_text = "".join(item[0] for item in data["diff"])
        assert diff_text == "TRIMMED VERSION"
        # But the recorded document paths still point at the canonical .txt
        # capture path, regardless of what the diff was actually built from.
        assert data["older_document"]["path"] == "co1/privacy_policy/1.txt"
        assert data["newer_document"]["path"] == "co1/privacy_policy/2.txt"

    def test_duplicate_date_captures_ignore_the_older_one(
        self, monkeypatch, tmp_path, caplog
    ):
        same_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
        texts = {
            "co1/privacy_policy/1.txt": "First same-day capture text content",
            "co1/privacy_policy/2.txt": "Second same-day capture text content",
            "co1/privacy_policy/3.txt": "Later capture with different content",
        }
        documents = [
            make_doc(ObjectId(), "co1/privacy_policy/1", same_date),
            make_doc(ObjectId(), "co1/privacy_policy/2", same_date),
            make_doc(
                ObjectId(),
                "co1/privacy_policy/3",
                datetime(2024, 2, 1, tzinfo=timezone.utc),
            ),
        ]
        self._setup(monkeypatch, tmp_path, documents)
        monkeypatch.setattr(
            changes_module, "get_gcs_bytes", lambda path, bucket: texts[path].encode()
        )
        saved = []
        monkeypatch.setattr(
            changes_module, "save_db", lambda docs, path: saved.extend(docs)
        )

        with caplog.at_level(logging.ERROR):
            changes_module.create_changes_json()

        # Only the later of the same-day pair survives, leaving 2 valid
        # captures -> exactly 1 pair.
        assert len(saved) == 1
        assert "Detected Duplicate date" in caplog.text

    def test_single_capture_produces_no_changes(self, monkeypatch, tmp_path, caplog):
        documents = [
            make_doc(
                ObjectId(),
                "co1/privacy_policy/1",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
        ]
        self._setup(monkeypatch, tmp_path, documents)
        saved = []
        monkeypatch.setattr(
            changes_module, "save_db", lambda docs, path: saved.extend(docs)
        )

        with caplog.at_level(logging.ERROR):
            changes_module.create_changes_json()

        assert saved == []
        assert "No potential changes" in caplog.text

    def test_existing_change_is_not_recreated(self, monkeypatch, tmp_path):
        id1, id2 = ObjectId(), ObjectId()
        documents = [
            make_doc(
                id1,
                "co1/privacy_policy/1",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
            make_doc(
                id2,
                "co1/privacy_policy/2",
                datetime(2024, 2, 1, tzinfo=timezone.utc),
            ),
        ]
        existing_change = {
            "company_id": "co1",
            "type": "privacy_policy",
            "older_document_id": id1,
            "newer_document_id": id2,
        }
        self._setup(monkeypatch, tmp_path, documents, changes_db=[existing_change])
        fetched = []
        monkeypatch.setattr(
            changes_module,
            "get_gcs_bytes",
            lambda path, bucket: fetched.append(path) or b"text",
        )
        saved = []
        monkeypatch.setattr(
            changes_module, "save_db", lambda docs, path: saved.extend(docs)
        )

        changes_module.create_changes_json()

        assert saved == []
        assert fetched == []

    def test_document_cache_avoids_refetching_the_same_path(
        self, monkeypatch, tmp_path
    ):
        texts = {
            "co1/privacy_policy/1.txt": "Shared original content across pairs",
            "co1/privacy_policy/2.txt": "Second version of the content here",
            "co1/privacy_policy/3.txt": "Third version of the content here",
        }
        documents = [
            make_doc(
                ObjectId(),
                "co1/privacy_policy/1",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
            make_doc(
                ObjectId(),
                "co1/privacy_policy/2",
                datetime(2024, 2, 1, tzinfo=timezone.utc),
            ),
            make_doc(
                ObjectId(),
                "co1/privacy_policy/3",
                datetime(2024, 3, 1, tzinfo=timezone.utc),
            ),
        ]
        self._setup(monkeypatch, tmp_path, documents)
        fetch_calls = []

        def fake_get_gcs_bytes(path, bucket):
            fetch_calls.append(path)
            return texts[path].encode()

        monkeypatch.setattr(changes_module, "get_gcs_bytes", fake_get_gcs_bytes)
        monkeypatch.setattr(changes_module, "save_db", lambda *a, **k: None)

        changes_module.create_changes_json()

        # Path "1.txt" is reused as the older document across two different
        # pairs -- the DocumentCache should mean each unique path is only
        # ever fetched once across the whole run.
        assert sorted(set(fetch_calls)) == sorted(texts.keys())
        assert len(fetch_calls) == len(set(fetch_calls))

    def test_idset_filters_out_other_companies_locally(self, monkeypatch, tmp_path):
        documents = [
            make_doc(
                ObjectId(),
                "co1/privacy_policy/1",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                company_id="co1",
            ),
            make_doc(
                ObjectId(),
                "co1/privacy_policy/2",
                datetime(2024, 2, 1, tzinfo=timezone.utc),
                company_id="co1",
            ),
            make_doc(
                ObjectId(),
                "co2/privacy_policy/1",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                company_id="co2",
            ),
            make_doc(
                ObjectId(),
                "co2/privacy_policy/2",
                datetime(2024, 2, 1, tzinfo=timezone.utc),
                company_id="co2",
            ),
        ]
        self._setup(monkeypatch, tmp_path, documents)
        monkeypatch.setattr(
            changes_module,
            "get_gcs_bytes",
            lambda path, bucket: b"some content words here today",
        )
        saved = []
        monkeypatch.setattr(
            changes_module, "save_db", lambda docs, path: saved.extend(docs)
        )

        changes_module.create_changes_json(idSet={"co1"})

        assert len(saved) == 1
        assert saved[0]["company_id"] == "co1"

    def test_multiple_groups_are_all_processed_when_run_in_parallel(
        self, monkeypatch, tmp_path
    ):
        # Regression guard for create_changes_json's ThreadPoolExecutor
        # dispatch across (company_id, type) groups: every independent group
        # must still get its own complete, correct output, not just the
        # first one submitted.
        CFG.MAX_WORKERS = 4
        texts = {
            "co1/privacy_policy/1.txt": "Co1 privacy version one content",
            "co1/privacy_policy/2.txt": "Co1 privacy version two content",
            "co2/terms_of_service/1.txt": "Co2 terms version one content",
            "co2/terms_of_service/2.txt": "Co2 terms version two content",
            "co3/privacy_policy/1.txt": "Co3 privacy version one content",
            "co3/privacy_policy/2.txt": "Co3 privacy version two content",
        }
        documents = [
            make_doc(
                ObjectId(),
                "co1/privacy_policy/1",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                company_id="co1",
                doc_type="privacy_policy",
            ),
            make_doc(
                ObjectId(),
                "co1/privacy_policy/2",
                datetime(2024, 2, 1, tzinfo=timezone.utc),
                company_id="co1",
                doc_type="privacy_policy",
            ),
            make_doc(
                ObjectId(),
                "co2/terms_of_service/1",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                company_id="co2",
                doc_type="terms_of_service",
            ),
            make_doc(
                ObjectId(),
                "co2/terms_of_service/2",
                datetime(2024, 2, 1, tzinfo=timezone.utc),
                company_id="co2",
                doc_type="terms_of_service",
            ),
            make_doc(
                ObjectId(),
                "co3/privacy_policy/1",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                company_id="co3",
                doc_type="privacy_policy",
            ),
            make_doc(
                ObjectId(),
                "co3/privacy_policy/2",
                datetime(2024, 2, 1, tzinfo=timezone.utc),
                company_id="co3",
                doc_type="privacy_policy",
            ),
        ]
        self._setup(monkeypatch, tmp_path, documents)
        monkeypatch.setattr(
            changes_module, "get_gcs_bytes", lambda path, bucket: texts[path].encode()
        )
        saved = []
        save_lock = threading.Lock()

        def fake_save_db(docs, path):
            with save_lock:
                saved.extend(docs)

        monkeypatch.setattr(changes_module, "save_db", fake_save_db)

        changes_module.create_changes_json()

        assert sorted(d["company_id"] for d in saved) == ["co1", "co2", "co3"]
        changes_files = list((tmp_path / "changes").rglob("*.CHANGES.json"))
        assert len(changes_files) == 3


# --------------------------------------------------------------------------
# create_changes_json -- Mongo backend
# --------------------------------------------------------------------------


class TestCreateChangesJsonMongo:
    def test_uses_aggregate_result_and_idset_filter(self, monkeypatch, tmp_path):
        """The Mongo branch's `.aggregate()` is a real MongoDB operation; a
        lightweight FakeCollection can't replicate real aggregation-pipeline
        semantics, so this test hands it canned `grouped_captures` data
        directly and only verifies (a) the $match stage is built correctly
        from idSet and (b) create_changes_json's consumption of the grouped
        result is correct. Verifying the pipeline executes correctly against
        a real MongoDB aggregation engine is out of scope here."""
        CFG.USE_MONGO = True
        CFG.IN_CLOUD = False
        CFG.USE_GCP = False
        CFG.CAPTURES_DIR = tmp_path
        CFG.CHANGES_DIR = Path("changes")
        CFG.DB_NAME = "dev_db"

        id1, id2 = ObjectId(), ObjectId()
        grouped = [
            {
                "_id": {"company_id": "co1", "type": "privacy_policy"},
                "captures": [
                    {
                        "_id": id1,
                        "path": "co1/privacy_policy/1",
                        "date_fetched": datetime(2024, 1, 1, tzinfo=timezone.utc),
                    },
                    {
                        "_id": id2,
                        "path": "co1/privacy_policy/2",
                        "date_fetched": datetime(2024, 2, 1, tzinfo=timezone.utc),
                    },
                ],
            }
        ]
        documents_collection = FakeCollection(aggregate_result=grouped)
        changes_collection = FakeCollection()
        fake_client = FakeMongoClient(
            {
                "dev_db": {
                    "documents_v2": documents_collection,
                    "changes": changes_collection,
                }
            }
        )
        monkeypatch.setattr(changes_module, "mongo_connect", lambda: fake_client)
        monkeypatch.setattr(
            changes_module,
            "get_gcs_bytes",
            lambda path, bucket: {
                "co1/privacy_policy/1.txt": b"old text content here today",
                "co1/privacy_policy/2.txt": b"new text content here today",
            }[path],
        )

        changes_module.create_changes_json(idSet={"co1"})

        assert documents_collection.aggregate_calls
        match_stage = documents_collection.aggregate_calls[0][0]["$match"]
        assert match_stage["company_id"] == {"$in": ["co1"]}
        assert len(changes_collection.inserted) == 1
        assert changes_collection.inserted[0]["older_document_id"] == id1
        assert changes_collection.inserted[0]["newer_document_id"] == id2

    def test_skips_creating_change_that_already_exists(self, monkeypatch, tmp_path):
        CFG.USE_MONGO = True
        CFG.IN_CLOUD = False
        CFG.USE_GCP = False
        CFG.CAPTURES_DIR = tmp_path
        CFG.CHANGES_DIR = Path("changes")
        CFG.DB_NAME = "dev_db"

        id1, id2 = ObjectId(), ObjectId()
        grouped = [
            {
                "_id": {"company_id": "co1", "type": "privacy_policy"},
                "captures": [
                    {
                        "_id": id1,
                        "path": "co1/privacy_policy/1",
                        "date_fetched": datetime(2024, 1, 1, tzinfo=timezone.utc),
                    },
                    {
                        "_id": id2,
                        "path": "co1/privacy_policy/2",
                        "date_fetched": datetime(2024, 2, 1, tzinfo=timezone.utc),
                    },
                ],
            }
        ]
        documents_collection = FakeCollection(aggregate_result=grouped)
        existing_change = {
            "company_id": "co1",
            "type": "privacy_policy",
            "older_document_id": id1,
            "newer_document_id": id2,
        }
        changes_collection = FakeCollection(docs=[existing_change])
        fake_client = FakeMongoClient(
            {
                "dev_db": {
                    "documents_v2": documents_collection,
                    "changes": changes_collection,
                }
            }
        )
        monkeypatch.setattr(changes_module, "mongo_connect", lambda: fake_client)
        fetched = []
        monkeypatch.setattr(
            changes_module,
            "get_gcs_bytes",
            lambda path, bucket: fetched.append(path) or b"text",
        )

        changes_module.create_changes_json()

        assert fetched == []
        assert changes_collection.inserted == []
