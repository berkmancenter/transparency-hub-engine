# Tests for source/process.py: ProcessCrawl, which turns a completed crawl's
# on-disk output into Document-collection records and decides whether each
# format is actually new content worth keeping.

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

import pytest

from source.globals import CFG
from source import process as process_module
from source.process import ProcessCrawl

TIMESTAMP = 1700000000


def make_crawl_result(crawl_name=None, **overrides):
    result = {
        "crawl_name": crawl_name or f"batch_1_{TIMESTAMP}",
        "objId": "co1",
        "pType": "privacy_policy",
        "urls": ["https://example.com/privacy"],
        "success": True,
    }
    result.update(overrides)
    return result


def _write_with_mtime(path: Path, content: str, mtime_offset: int) -> None:
    """Writes content and sets an explicit, distinguishable mtime -- avoids
    flaky ordering assertions that would otherwise depend on filesystem
    timestamp resolution / write timing."""
    path.write_text(content)
    stamp = TIMESTAMP + mtime_offset
    os.utime(path, (stamp, stamp))


def _write_pages_jsonl(j_dir: Path, pages: list[tuple[str, str]]) -> None:
    """Writes a pages.jsonl mapping each (url, text) the way the crawl's
    --text to-pages output does, so merge_txt_files can map detached .txt
    pages back to their seed urls."""
    (j_dir / "pages.jsonl").write_text(
        "\n".join(json.dumps({"url": url, "text": text}) for url, text in pages)
    )


@pytest.fixture
def crawl_dirs(cfg_dirs):
    crawl_result = make_crawl_result()
    c_dir = CFG.CRAWL_DIR / "collections" / crawl_result["crawl_name"]
    a_dir = c_dir / "archive"
    j_dir = c_dir / "pages"
    a_dir.mkdir(parents=True)
    j_dir.mkdir(parents=True)
    return crawl_result, c_dir, a_dir, j_dir


@pytest.fixture
def processor(crawl_dirs, fake_collection):
    crawl_result, *_ = crawl_dirs
    CFG.USE_MONGO = True
    return ProcessCrawl(crawl_result, fake_collection, googleBucket=None)


class TestInit:
    def test_sets_up_directories_and_item_info(self, crawl_dirs, fake_collection):
        crawl_result, c_dir, a_dir, j_dir = crawl_dirs

        proc = ProcessCrawl(crawl_result, fake_collection)

        assert proc.cDir == c_dir
        assert proc.aDir == a_dir
        assert proc.jDir == j_dir
        assert proc.sDir.is_dir()
        assert proc.cTimestamp == str(TIMESTAMP)
        assert proc.genItemInfo["date_fetched"] == datetime.fromtimestamp(
            TIMESTAMP, tz=timezone.utc
        )
        assert proc.genItemInfo["company_id"] == "co1"
        assert proc.genItemInfo["type"] == "privacy_policy"
        assert "original_url" not in proc.genItemInfo
        assert proc.failCounter == 0
        assert proc.confirmedNew is False
        assert proc.txtDirectlyChanged is None
        assert proc.formats == {}
        assert proc.results == []
        assert proc.fileNames == []


class TestCreateWarcJson:
    def test_dedupes_by_url_and_text(self, processor, crawl_dirs):
        _, _, _, j_dir = crawl_dirs
        pages = j_dir / "pages.jsonl"
        extra = j_dir / "extraPages.jsonl"
        pages.write_text(
            "\n".join(
                [
                    json.dumps({"url": "https://a.example/1", "text": "Same content"}),
                    json.dumps({"url": "https://a.example/2", "text": "Same content"}),
                    json.dumps(
                        {"url": "https://a.example/3", "text": "Different content"}
                    ),
                ]
            )
        )
        extra.write_text(
            json.dumps({"url": "https://a.example/4", "text": "Different content"})
        )

        result = processor.create_warc_json(pages, extra)

        assert result["total_unique_content"] == 2
        # Only the *first* url to introduce a piece of content is counted as
        # "unique" -- later urls sharing that content get folded into the
        # existing record's url list without growing total_urls. That makes
        # total_urls a count of content-defining urls, not of all urls seen.
        assert result["total_urls"] == 2
        assert result["records"] == [
            {
                "urls": ["https://a.example/1", "https://a.example/2"],
                "content": "Same content",
            },
            {
                "urls": ["https://a.example/3", "https://a.example/4"],
                "content": "Different content",
            },
        ]

    def test_skips_blank_and_malformed_lines(self, processor, crawl_dirs):
        _, _, _, j_dir = crawl_dirs
        pages = j_dir / "pages.jsonl"
        extra = j_dir / "extraPages.jsonl"
        pages.write_text(
            "\n".join(
                [
                    "",
                    json.dumps({"url": "https://a.example/1", "text": "Hello world"}),
                    "{not valid json",
                ]
            )
        )
        extra.write_text("")

        result = processor.create_warc_json(pages, extra)

        assert result["total_unique_content"] == 1
        assert result["records"] == [
            {"urls": ["https://a.example/1"], "content": "Hello world"}
        ]

    def test_skips_pages_missing_url_or_text(self, processor, crawl_dirs):
        _, _, _, j_dir = crawl_dirs
        pages = j_dir / "pages.jsonl"
        extra = j_dir / "extraPages.jsonl"
        pages.write_text(
            "\n".join(
                [
                    json.dumps({"url": "https://a.example/1"}),
                    json.dumps({"text": "orphan text"}),
                    json.dumps({"url": "https://a.example/2", "text": "Valid"}),
                ]
            )
        )
        extra.write_text("")

        result = processor.create_warc_json(pages, extra)

        assert result["total_unique_content"] == 1
        assert result["records"][0]["urls"] == ["https://a.example/2"]


class TestCheckForFailedCaptures:
    def test_notifies_slack_on_404_status(self, processor, crawl_dirs, monkeypatch):
        _, _, _, j_dir = crawl_dirs
        (j_dir / "pages.jsonl").write_text(
            json.dumps({"url": "https://a.example/missing", "status": 404})
        )
        (j_dir / "extraPages.jsonl").write_text("")

        messages = []
        monkeypatch.setattr(
            process_module, "notify_slack", lambda msg: messages.append(msg)
        )

        processor.check_for_failed_captures()

        assert len(messages) == 1
        assert "https://a.example/missing" in messages[0]
        assert "404" in messages[0]

    def test_collects_404s_from_both_page_files(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _, _, j_dir = crawl_dirs
        (j_dir / "pages.jsonl").write_text(
            json.dumps({"url": "https://a.example/1", "status": 200})
        )
        (j_dir / "extraPages.jsonl").write_text(
            json.dumps({"url": "https://a.example/2", "status": 404})
        )

        messages = []
        monkeypatch.setattr(
            process_module, "notify_slack", lambda msg: messages.append(msg)
        )

        processor.check_for_failed_captures()

        assert len(messages) == 1
        assert "https://a.example/2" in messages[0]
        assert "https://a.example/1" not in messages[0]

    def test_no_notification_when_no_404s(self, processor, crawl_dirs, monkeypatch):
        _, _, _, j_dir = crawl_dirs
        (j_dir / "pages.jsonl").write_text(
            json.dumps({"url": "https://a.example/1", "status": 200})
        )
        (j_dir / "extraPages.jsonl").write_text("")

        messages = []
        monkeypatch.setattr(
            process_module, "notify_slack", lambda msg: messages.append(msg)
        )

        processor.check_for_failed_captures()

        assert messages == []

    def test_no_notification_when_page_files_missing(
        self, processor, crawl_dirs, monkeypatch
    ):
        messages = []
        monkeypatch.setattr(
            process_module, "notify_slack", lambda msg: messages.append(msg)
        )

        processor.check_for_failed_captures()

        assert messages == []

    def test_skips_malformed_lines(self, processor, crawl_dirs, monkeypatch):
        _, _, _, j_dir = crawl_dirs
        (j_dir / "pages.jsonl").write_text(
            "\n".join(
                [
                    "{not valid json",
                    json.dumps({"url": "https://a.example/1", "status": 404}),
                ]
            )
        )
        (j_dir / "extraPages.jsonl").write_text("")

        messages = []
        monkeypatch.setattr(
            process_module, "notify_slack", lambda msg: messages.append(msg)
        )

        processor.check_for_failed_captures()

        assert len(messages) == 1
        assert "https://a.example/1" in messages[0]

    def test_notify_slack_exception_is_caught(self, processor, crawl_dirs, monkeypatch):
        _, _, _, j_dir = crawl_dirs
        (j_dir / "pages.jsonl").write_text(
            json.dumps({"url": "https://a.example/missing", "status": 404})
        )
        (j_dir / "extraPages.jsonl").write_text("")

        def boom(msg):
            raise RuntimeError("slack is down")

        monkeypatch.setattr(process_module, "notify_slack", boom)

        # Must not raise
        processor.check_for_failed_captures()


class TestProcessFormat:
    def test_txt_found_and_new_sets_confirmed_new(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.txt").write_text("hello")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)

        processor.process_format(("txt", a_dir))

        assert processor.confirmedNew is True
        assert processor.failCounter == 0
        # process_format only stages the format; process_crawl does the merge
        assert processor.results == []
        assert processor.formats["txt"] == {
            "extension": "txt",
            "original_url": processor.crawlResult["urls"],
        }
        copied = processor.sDir / f"{processor.cTimestamp}.txt"
        assert copied.exists()
        assert processor.fileNames == [copied]

    def test_txt_merges_multiple_pages_in_seed_url_order(
        self, crawl_dirs, fake_collection, monkeypatch
    ):
        # A crawl with more than one seed url produces one .txt per page --
        # all of them must be merged into the captured document, in the
        # doc_urls entry's url order. The mtimes deliberately contradict
        # that order (the second seed finished loading first), so this only
        # passes if merge order follows the seed urls, not load timing.
        _, _c_dir, a_dir, j_dir = crawl_dirs
        urls = ["https://example.com/one", "https://example.com/two"]
        _write_with_mtime(a_dir / "a_page.txt", "second page text", 100)
        _write_with_mtime(a_dir / "z_page.txt", "first page text", 200)
        _write_pages_jsonl(
            j_dir, [(urls[1], "second page text"), (urls[0], "first page text")]
        )
        processor = ProcessCrawl(
            make_crawl_result(urls=urls), fake_collection, googleBucket=None
        )
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)

        processor.process_format(("txt", a_dir))

        copied = processor.sDir / f"{processor.cTimestamp}.txt"
        assert copied.read_text() == "first page text\n\nsecond page text"

    def test_second_pvalue_sharing_a_crawl_does_not_get_corrupted_txt(
        self, crawl_dirs, fake_collection, monkeypatch
    ):
        # Mirrors what pipeline.process_document actually does when
        # format_data collates two pValues onto the same url group: a
        # separate ProcessCrawl per pValue, all sharing the same crawl_name
        # (and therefore the same aDir). The second pValue's merge must not
        # re-glob and fold in the first pValue's already-merged output.
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "a_page.txt").write_text("first")
        (a_dir / "b_page.txt").write_text("second")

        crawl_result_a = make_crawl_result(objId="co-a", pType="privacy_policy")
        crawl_result_b = make_crawl_result(objId="co-b", pType="terms_of_service")
        processor_a = ProcessCrawl(crawl_result_a, fake_collection, googleBucket=None)
        processor_b = ProcessCrawl(crawl_result_b, fake_collection, googleBucket=None)
        monkeypatch.setattr(processor_a, "verify_content", lambda fmt, url, path: True)
        monkeypatch.setattr(processor_b, "verify_content", lambda fmt, url, path: True)

        processor_a.process_format(("txt", a_dir))
        processor_b.process_format(("txt", a_dir))

        assert (
            processor_a.sDir / f"{processor_a.cTimestamp}.txt"
        ).read_text() == "first\n\nsecond"
        assert (
            processor_b.sDir / f"{processor_b.cTimestamp}.txt"
        ).read_text() == "first\n\nsecond"

    def test_original_url_preserves_given_multi_url_order(
        self, crawl_dirs, fake_collection, monkeypatch
    ):
        # original_url is stored as a list going forward -- it must reflect
        # the seed urls in the order the doc_urls entry gave them (not
        # alphabetical, or any other reordering).
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        ordered_urls = [
            "https://example.com/z",
            "https://example.com/a",
            "https://example.com/m",
        ]
        crawl_result = make_crawl_result(urls=ordered_urls)
        processor = ProcessCrawl(crawl_result, fake_collection, googleBucket=None)
        (a_dir / "capture.txt").write_text("hello")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)

        processor.process_format(("txt", a_dir))

        assert processor.formats["txt"]["original_url"] == ordered_urls

    def test_txt_not_new_increments_fail_counter(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.txt").write_text("hello")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: False)

        processor.process_format(("txt", a_dir))

        assert processor.confirmedNew is False
        assert processor.failCounter == 1
        assert processor.formats == {}
        assert processor.results == []

    def test_missing_file_increments_fail_counter(self, processor, crawl_dirs):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        # No .html file was created for this crawl.

        processor.process_format(("html", a_dir))

        assert processor.failCounter == 1
        assert processor.formats == {}
        assert processor.results == []

    def test_missing_txt_files_increments_fail_counter(self, processor, crawl_dirs):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        # No .txt file was created for this crawl.

        processor.process_format(("txt", a_dir))

        assert processor.failCounter == 1
        assert processor.formats == {}
        assert processor.results == []


class TestMergeTxtFiles:
    def test_merges_in_seed_url_order_and_writes_under_cDir(
        self, crawl_dirs, fake_collection
    ):
        # Written under self.cDir (this crawl's own scratch dir), NOT
        # self.sDir (the persistent captures dir -- process_format only
        # copies it there once verify_content confirms a real change, so
        # writing it there directly would leave a stray file behind on
        # every rejected/unchanged capture too) and NOT iDir (iDir gets
        # re-globbed by every pValue collated onto this crawl, so writing
        # the merge output there would let a later pValue's merge re-ingest
        # an earlier pValue's already-merged file). Filenames and mtimes are
        # both deliberately reversed relative to the seed urls, so this only
        # passes if merge order actually follows the seed urls.
        _, c_dir, a_dir, j_dir = crawl_dirs
        urls = ["https://example.com/one", "https://example.com/two"]
        _write_with_mtime(a_dir / "a_page.txt", "second", 100)
        _write_with_mtime(a_dir / "b_page.txt", "first", 200)
        _write_pages_jsonl(j_dir, [(urls[1], "second"), (urls[0], "first")])
        processor = ProcessCrawl(
            make_crawl_result(urls=urls), fake_collection, googleBucket=None
        )

        mergedPath = processor.merge_txt_files(a_dir)

        assert mergedPath == c_dir / f"{processor.cTimestamp}.merged.txt"
        assert mergedPath.read_text() == "first\n\nsecond"

    def test_same_pages_merge_identically_regardless_of_load_order(
        self, crawl_dirs, fake_collection
    ):
        # Regression guard for the false "new version" captures: two crawls
        # of identical pages that finished loading in a different order
        # (different mtimes and pages.jsonl order) must merge to the exact
        # same text, so change detection never sees a reorder as a change.
        _, _c_dir, a_dir, j_dir = crawl_dirs
        urls = [
            "https://example.com/one",
            "https://example.com/two",
            "https://example.com/three",
        ]
        texts = ["page one", "page two", "page three"]
        processor = ProcessCrawl(
            make_crawl_result(urls=urls), fake_collection, googleBucket=None
        )

        merged = []
        for loadOrder in ([0, 1, 2], [2, 0, 1]):
            for f in a_dir.glob("*.txt"):
                f.unlink()
            for offset, i in enumerate(loadOrder):
                _write_with_mtime(a_dir / f"page_{i}.txt", texts[i], offset)
            _write_pages_jsonl(j_dir, [(urls[i], texts[i]) for i in loadOrder])
            merged.append(processor.merge_txt_files(a_dir).read_text())

        assert merged[0] == merged[1] == "page one\n\npage two\n\npage three"

    def test_matches_seed_url_despite_trailing_slash_or_fragment(
        self, crawl_dirs, fake_collection
    ):
        _, _c_dir, a_dir, j_dir = crawl_dirs
        urls = ["https://example.com/one", "https://example.com/two/"]
        (a_dir / "a.txt").write_text("second")
        (a_dir / "b.txt").write_text("first")
        _write_pages_jsonl(
            j_dir,
            [
                ("https://example.com/two", "second"),
                ("https://example.com/one#x", "first"),
            ],
        )
        processor = ProcessCrawl(
            make_crawl_result(urls=urls), fake_collection, googleBucket=None
        )

        assert processor.merge_txt_files(a_dir).read_text() == "first\n\nsecond"

    def test_unmatched_pages_go_last_in_deterministic_text_order(
        self, crawl_dirs, fake_collection
    ):
        # Pages that can't be mapped to a seed url (no pages.jsonl entry)
        # follow every matched page, ordered by their own text -- never by
        # mtime or filename.
        _, _c_dir, a_dir, j_dir = crawl_dirs
        urls = ["https://example.com/one"]
        _write_with_mtime(a_dir / "a.txt", "zebra", 100)
        _write_with_mtime(a_dir / "b.txt", "apple", 200)
        _write_with_mtime(a_dir / "c.txt", "seed page", 300)
        _write_pages_jsonl(j_dir, [(urls[0], "seed page")])
        processor = ProcessCrawl(
            make_crawl_result(urls=urls), fake_collection, googleBucket=None
        )

        assert (
            processor.merge_txt_files(a_dir).read_text()
            == "seed page\n\napple\n\nzebra"
        )

    def test_does_not_write_into_the_globbed_directory(self, processor, crawl_dirs):
        # Regression guard for the cross-pValue corruption bug: a second call
        # against the same iDir must see the same two source files, not the
        # first call's own merged output folded back in as a third page.
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        _write_with_mtime(a_dir / "a_page.txt", "first", 100)
        _write_with_mtime(a_dir / "b_page.txt", "second", 200)

        processor.merge_txt_files(a_dir)
        secondMerge = processor.merge_txt_files(a_dir)

        assert secondMerge.read_text() == "first\n\nsecond"

    def test_single_file_is_returned_unchanged(self, processor, crawl_dirs):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.txt").write_text("only page")

        mergedPath = processor.merge_txt_files(a_dir)

        assert mergedPath.read_text() == "only page"

    def test_raises_when_no_txt_files_found(self, processor, crawl_dirs):
        _, _c_dir, a_dir, _j_dir = crawl_dirs

        with pytest.raises(Exception):
            processor.merge_txt_files(a_dir)

    def test_confirmed_new_short_circuits_verify_content(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.html").write_text("<html></html>")
        processor.confirmedNew = True
        calls = []
        monkeypatch.setattr(
            processor,
            "verify_content",
            lambda fmt, url, path: calls.append(1) or False,
        )

        processor.process_format(("html", a_dir))

        assert calls == []
        assert processor.formats["html"]["extension"] == "html"

    def test_wacz_bypasses_verify_content_entirely(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, c_dir, _a_dir, _j_dir = crawl_dirs
        (c_dir / "archive.wacz").write_bytes(b"fake wacz")
        calls = []
        monkeypatch.setattr(
            processor,
            "verify_content",
            lambda fmt, url, path: calls.append(1) or False,
        )

        processor.process_format(("wacz", c_dir))

        assert calls == []
        assert processor.results == []
        assert processor.formats["warc"] == {
            "extension": "wacz",
            "original_url": processor.crawlResult["urls"],
        }

    def test_warc_json_branch_builds_and_writes_file(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, _a_dir, j_dir = crawl_dirs
        (j_dir / "pages.jsonl").write_text(
            json.dumps(
                {
                    "url": "https://a.example",
                    "text": "hello world this is a test with enough words",
                }
            )
        )
        (j_dir / "extraPages.jsonl").write_text("")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)

        processor.process_format(("warc.json", j_dir))

        assert processor.failCounter == 0
        assert processor.formats["warc.json"]["extension"] == "warc.json"
        written = j_dir / f"{processor.cTimestamp}.warc.json"
        assert written.exists()
        copied = processor.sDir / f"{processor.cTimestamp}.warc.json"
        assert copied.exists()


class TestVerifyContent:
    ORIGINAL_URLS: ClassVar[list[str]] = ["https://example.com/privacy"]

    def test_returns_false_when_validation_fails(self, processor, monkeypatch):
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: False)

        assert (
            processor.verify_content("txt", self.ORIGINAL_URLS, Path("dummy.txt"))
            is False
        )

    def test_returns_true_when_no_previous_capture_mongo(self, processor, monkeypatch):
        CFG.USE_MONGO = True
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)

        assert (
            processor.verify_content("txt", self.ORIGINAL_URLS, Path("dummy.txt"))
            is True
        )

    def test_compares_against_existing_capture_mongo(self, processor, monkeypatch):
        CFG.USE_MONGO = True
        processor.collection.docs.append(
            {
                "company_id": "co1",
                "type": "privacy_policy",
                "date_fetched": "2024-01-01",
                "path": "co1/privacy_policy/123",
                "formats": {
                    "txt": {"extension": "txt", "original_url": self.ORIGINAL_URLS}
                },
            }
        )
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)
        monkeypatch.setattr(process_module, "compare_files", lambda *a, **k: True)

        assert (
            processor.verify_content("txt", self.ORIGINAL_URLS, Path("dummy.txt"))
            is True
        )

    def test_uses_local_find_when_use_mongo_false(self, processor, monkeypatch):
        CFG.USE_MONGO = False
        processor.collection = [
            {
                "company_id": "co1",
                "type": "privacy_policy",
                "date_fetched": "2024-01-01",
                "path": "co1/privacy_policy/123",
                "formats": {
                    "txt": {"extension": "txt", "original_url": self.ORIGINAL_URLS}
                },
            }
        ]
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)
        monkeypatch.setattr(process_module, "compare_files", lambda *a, **k: False)

        assert (
            processor.verify_content("txt", self.ORIGINAL_URLS, Path("dummy.txt"))
            is False
        )

    def test_matches_previous_capture_with_dotted_format_key_mongo(
        self, processor, monkeypatch
    ):
        """Regression test: "warc.json" contains a literal "." in its
        formats-dict key. A "formats.warc.json" query filter would get
        parsed by MongoDB's dot-notation as the nested path
        formats->warc->json, which this schema never has, so it would never
        match -- verify_content must find candidates by (company_id, type)
        only and check format membership in Python instead."""
        CFG.USE_MONGO = True
        processor.collection.docs.append(
            {
                "company_id": "co1",
                "type": "privacy_policy",
                "date_fetched": "2024-01-01",
                "path": "co1/privacy_policy/123",
                "formats": {
                    "warc.json": {
                        "extension": "warc.json",
                        "original_url": self.ORIGINAL_URLS,
                    }
                },
            }
        )
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)
        # If verify_content wrongly took the "no previous capture" branch
        # (the bug), it would return True regardless of this.
        monkeypatch.setattr(process_module, "compare_files", lambda *a, **k: False)

        assert (
            processor.verify_content(
                "warc.json", self.ORIGINAL_URLS, Path("dummy.warc.json")
            )
            is False
        )

    def test_matches_previous_capture_with_dotted_format_key_local_mode(
        self, processor, monkeypatch
    ):
        CFG.USE_MONGO = False
        processor.collection = [
            {
                "company_id": "co1",
                "type": "privacy_policy",
                "date_fetched": "2024-01-01",
                "path": "co1/privacy_policy/123",
                "formats": {
                    "warc.json": {
                        "extension": "warc.json",
                        "original_url": self.ORIGINAL_URLS,
                    }
                },
            }
        ]
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)
        monkeypatch.setattr(process_module, "compare_files", lambda *a, **k: False)

        assert (
            processor.verify_content(
                "warc.json", self.ORIGINAL_URLS, Path("dummy.warc.json")
            )
            is False
        )

    def test_skips_candidates_missing_the_requested_format(
        self, processor, monkeypatch
    ):
        # The most recent capture overall (2024-02-01) doesn't have a "txt"
        # format -- bestMatch should be the most recent capture that DOES
        # have it, not just the single most recent document overall.
        CFG.USE_MONGO = True
        processor.collection.docs.extend(
            [
                {
                    "company_id": "co1",
                    "type": "privacy_policy",
                    "date_fetched": "2024-01-01",
                    "path": "co1/privacy_policy/1",
                    "formats": {
                        "txt": {"extension": "txt", "original_url": self.ORIGINAL_URLS}
                    },
                },
                {
                    "company_id": "co1",
                    "type": "privacy_policy",
                    "date_fetched": "2024-02-01",
                    "path": "co1/privacy_policy/2",
                    "formats": {
                        "html": {
                            "extension": "html",
                            "original_url": self.ORIGINAL_URLS,
                        }
                    },
                },
            ]
        )
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)
        seen_paths = []
        monkeypatch.setattr(
            process_module,
            "compare_files",
            lambda local, gcp_path, fmt, bucket: seen_paths.append(gcp_path) or True,
        )

        processor.verify_content("txt", self.ORIGINAL_URLS, Path("dummy.txt"))

        assert seen_paths == ["co1/privacy_policy/1.txt"]

    def test_compare_exception_returns_false(self, processor, monkeypatch, caplog):
        CFG.USE_MONGO = True
        processor.collection.docs.append(
            {
                "company_id": "co1",
                "type": "privacy_policy",
                "date_fetched": "2024-01-01",
                "path": "co1/privacy_policy/123",
                "formats": {
                    "txt": {"extension": "txt", "original_url": self.ORIGINAL_URLS}
                },
            }
        )
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)

        def boom(*a, **k):
            raise RuntimeError("gcs unreachable")

        monkeypatch.setattr(process_module, "compare_files", boom)

        assert (
            processor.verify_content("txt", self.ORIGINAL_URLS, Path("dummy.txt"))
            is False
        )


class TestReconstructMissingTxt:
    def test_reconstructs_from_html_when_txt_missing(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.html").write_text("<html></html>")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)
        processor.process_format(("html", a_dir))
        assert "txt" not in processor.formats

        monkeypatch.setattr(
            process_module,
            "reconstruct_text_from_format",
            lambda raw, fmt, ext, url: "Reconstructed policy text",
        )
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)

        processor.reconstruct_missing_txt()

        assert processor.formats["txt"] == {
            "extension": "txt",
            "original_url": processor.crawlResult["urls"],
        }
        newFile = processor.sDir / f"{processor.cTimestamp}.txt"
        assert newFile.exists()
        assert newFile.read_text() == "Reconstructed policy text"
        assert newFile in processor.fileNames

    def test_respects_reconstruction_priority_order(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, c_dir, a_dir, j_dir = crawl_dirs
        (a_dir / "capture.html").write_text("<html></html>")
        (a_dir / "capture.pdf").write_bytes(b"%PDF-fake")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)
        processor.process_format(("html", a_dir))
        processor.process_format(("pdf", a_dir))
        assert set(processor.formats.keys()) == {"html", "pdf"}

        seen_formats = []

        def fake_reconstruct(raw, fmt, ext, url):
            seen_formats.append(fmt)
            return "Some reconstructed text"

        monkeypatch.setattr(
            process_module, "reconstruct_text_from_format", fake_reconstruct
        )
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)

        processor.reconstruct_missing_txt()

        # html outranks pdf in RECONSTRUCTION_FORMAT_PRIORITY, so it's tried
        # (and succeeds) first -- pdf is never attempted.
        assert seen_formats == ["html"]
        assert processor.formats["txt"]["extension"] == "txt"

    def test_falls_back_to_next_format_when_first_fails_validation(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.html").write_text("<html></html>")
        (a_dir / "capture.pdf").write_bytes(b"%PDF-fake")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)
        processor.process_format(("html", a_dir))
        processor.process_format(("pdf", a_dir))

        def fake_reconstruct(raw, fmt, ext, url):
            return f"text from {fmt}"

        monkeypatch.setattr(
            process_module, "reconstruct_text_from_format", fake_reconstruct
        )
        # html's reconstructed text fails verification (bad quality/classifier
        # score or no real change); pdf's succeeds. Both attempts write to
        # the same {timestamp}.txt path, so distinguish by content instead.
        monkeypatch.setattr(
            processor,
            "verify_content",
            lambda fmt, url, path: path.read_text() == "text from pdf",
        )

        processor.reconstruct_missing_txt()

        assert processor.formats["txt"]["extension"] == "txt"
        newFile = processor.sDir / f"{processor.cTimestamp}.txt"
        assert newFile.read_text() == "text from pdf"
        # The rejected html-sourced attempt should have been cleaned up, not
        # left behind as a stray file.
        assert newFile.exists()

    def test_no_reconstruction_source_available_leaves_formats_unchanged(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, j_dir = crawl_dirs
        (j_dir / "pages.jsonl").write_text(
            json.dumps({"url": "https://a.example", "text": "hello world"})
        )
        (j_dir / "extraPages.jsonl").write_text("")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: False)
        processor.process_format(("warc.json", j_dir))
        assert "warc.json" not in processor.formats

        processor.reconstruct_missing_txt()

        assert "txt" not in processor.formats
        assert processor.fileNames == []

    def test_empty_extracted_text_tries_next_format(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.html").write_text("<html></html>")
        (a_dir / "capture.pdf").write_bytes(b"%PDF-fake")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)
        processor.process_format(("html", a_dir))
        processor.process_format(("pdf", a_dir))

        def fake_reconstruct(raw, fmt, ext, url):
            return "" if fmt == "html" else "text from pdf"

        monkeypatch.setattr(
            process_module, "reconstruct_text_from_format", fake_reconstruct
        )
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)

        processor.reconstruct_missing_txt()

        assert processor.formats["txt"]["extension"] == "txt"
        newFile = processor.sDir / f"{processor.cTimestamp}.txt"
        assert newFile.read_text() == "text from pdf"

    def test_extraction_exception_tries_next_format(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.html").write_text("<html></html>")
        (a_dir / "capture.pdf").write_bytes(b"%PDF-fake")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)
        processor.process_format(("html", a_dir))
        processor.process_format(("pdf", a_dir))

        def fake_reconstruct(raw, fmt, ext, url):
            if fmt == "html":
                raise RuntimeError("extraction blew up")
            return "text from pdf"

        monkeypatch.setattr(
            process_module, "reconstruct_text_from_format", fake_reconstruct
        )
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)

        processor.reconstruct_missing_txt()

        assert processor.formats["txt"]["extension"] == "txt"

    def test_verify_content_exception_tries_next_format(
        self, processor, crawl_dirs, monkeypatch
    ):
        """verify_content isn't guarded by reconstruct_text_from_format's own
        try/except -- an unexpected error there (e.g. a transient Mongo
        error) must rule out this candidate format and move on, not
        propagate out and lose every other already-validated format for
        this document."""
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.html").write_text("<html></html>")
        (a_dir / "capture.pdf").write_bytes(b"%PDF-fake")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)
        processor.process_format(("html", a_dir))
        processor.process_format(("pdf", a_dir))

        monkeypatch.setattr(
            process_module,
            "reconstruct_text_from_format",
            lambda raw, fmt, ext, url: f"text from {fmt}",
        )

        def flaky_verify_content(fmt, url, path):
            if path.read_text() == "text from html":
                raise RuntimeError("transient mongo error")
            return True

        monkeypatch.setattr(processor, "verify_content", flaky_verify_content)

        processor.reconstruct_missing_txt()

        assert processor.formats["txt"]["extension"] == "txt"
        newFile = processor.sDir / f"{processor.cTimestamp}.txt"
        assert newFile.read_text() == "text from pdf"


class TestProcessCrawlOrchestration:
    def test_attempts_wacz_when_not_all_formats_failed(self, processor, monkeypatch):
        calls = []

        def fake_process_format(format_obj):
            fmt = format_obj[0]
            calls.append(fmt)
            if fmt not in ("html", "wacz"):
                processor.failCounter += 1

        monkeypatch.setattr(processor, "process_format", fake_process_format)

        processor.process_crawl()

        assert calls == ["txt", "warc.json", "html", "pdf", "wacz"]
        assert processor.failCounter == 3

    def test_skips_wacz_when_every_format_failed(self, processor, monkeypatch):
        calls = []

        def fake_process_format(format_obj):
            calls.append(format_obj[0])
            processor.failCounter += 1

        monkeypatch.setattr(processor, "process_format", fake_process_format)

        processor.process_crawl()

        assert calls == ["txt", "warc.json", "html", "pdf"]

    def test_returns_accumulated_results_and_filenames(self, processor, monkeypatch):
        def fake_process_format(format_obj):
            processor.results.append({"format": format_obj[0]})
            processor.fileNames.append(Path(f"{format_obj[0]}.file"))

        monkeypatch.setattr(processor, "process_format", fake_process_format)

        results, file_names = processor.process_crawl()

        assert len(results) == 5
        assert len(file_names) == 5

    def test_merges_successful_formats_into_a_single_document(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, c_dir, a_dir, j_dir = crawl_dirs
        (a_dir / "capture.txt").write_text("hello")
        (a_dir / "capture.html").write_text("<html></html>")
        (a_dir / "capture.pdf").write_bytes(b"%PDF-fake")
        (j_dir / "pages.jsonl").write_text(
            json.dumps({"url": "https://a.example", "text": "hello world"})
        )
        (j_dir / "extraPages.jsonl").write_text("")
        (c_dir / "archive.wacz").write_bytes(b"fake wacz")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)

        results, file_names = processor.process_crawl()

        assert len(results) == 1
        doc = results[0]
        assert doc["company_id"] == "co1"
        assert doc["type"] == "privacy_policy"
        assert "." not in Path(doc["path"]).name
        assert doc["public_url"].endswith(doc["path"])
        assert set(doc["formats"].keys()) == {"txt", "html", "pdf", "warc.json", "warc"}
        assert doc["formats"]["warc"] == {
            "extension": "wacz",
            "original_url": processor.crawlResult["urls"],
        }
        assert len(file_names) == 5

    def test_backfills_txt_when_crawl_produced_no_txt_directly(
        self, processor, crawl_dirs, monkeypatch
    ):
        # No capture.txt this time -- only html is available.
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.html").write_text("<html></html>")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: True)
        monkeypatch.setattr(
            process_module,
            "reconstruct_text_from_format",
            lambda raw, fmt, ext, url: "Reconstructed from html",
        )

        results, file_names = processor.process_crawl()

        assert len(results) == 1
        doc = results[0]
        assert doc["formats"]["txt"] == {
            "extension": "txt",
            "original_url": processor.crawlResult["urls"],
        }
        newTxt = processor.sDir / f"{processor.cTimestamp}.txt"
        assert newTxt in file_names
        assert newTxt.read_text() == "Reconstructed from html"

    def test_skips_remaining_formats_when_directly_captured_txt_is_unchanged(
        self, processor, crawl_dirs, monkeypatch
    ):
        # A real .txt was captured (not reconstructed), but comparison finds no
        # change -- html/pdf/warc.json/wacz would never be used for this document,
        # so process_crawl should never even look for them.
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.txt").write_text("hello")
        (a_dir / "capture.html").write_text("<html></html>")
        monkeypatch.setattr(processor, "verify_content", lambda fmt, url, path: False)

        calls = []
        real_process_format = processor.process_format

        def tracking_process_format(format_obj):
            calls.append(format_obj[0])
            real_process_format(format_obj)

        monkeypatch.setattr(processor, "process_format", tracking_process_format)

        results, file_names = processor.process_crawl()

        assert calls == ["txt"]
        assert processor.txtDirectlyChanged is False
        assert results == []
        assert file_names == []

    def test_does_not_skip_remaining_formats_when_txt_check_errors(
        self, processor, crawl_dirs, monkeypatch
    ):
        # verify_content raising (e.g. a transient Mongo error) is an ambiguous
        # outcome, not a confirmed non-change -- must not be treated the same as
        # test_skips_remaining_formats_when_directly_captured_txt_is_unchanged.
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.txt").write_text("hello")
        (a_dir / "capture.html").write_text("<html></html>")

        def flaky_verify_content(fmt, url, path):
            if fmt == "txt":
                raise RuntimeError("transient mongo error")
            return True

        monkeypatch.setattr(processor, "verify_content", flaky_verify_content)

        calls = []
        real_process_format = processor.process_format

        def tracking_process_format(format_obj):
            calls.append(format_obj[0])
            real_process_format(format_obj)

        monkeypatch.setattr(processor, "process_format", tracking_process_format)

        processor.process_crawl()

        assert processor.txtDirectlyChanged is None
        assert "html" in calls

    def test_does_not_attempt_backfill_when_nothing_was_captured(
        self, processor, monkeypatch
    ):
        # Every format fails to be found at all -- process.py should not try
        # to reconstruct a .txt from thin air.
        calls = []
        monkeypatch.setattr(
            process_module,
            "reconstruct_text_from_format",
            lambda *a, **k: calls.append(1) or "",
        )

        results, file_names = processor.process_crawl()

        assert results == []
        assert file_names == []
        assert calls == []
