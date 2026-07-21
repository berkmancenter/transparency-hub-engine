# Tests for source/process.py: ProcessCrawl, which turns a completed crawl's
# on-disk output into Document-collection records and decides whether each
# format is actually new content worth keeping.

import json
from datetime import datetime, timezone
from pathlib import Path

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
        "url": "https://example.com/privacy",
        "success": True,
    }
    result.update(overrides)
    return result


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
        assert proc.genItemInfo["original_url"] == crawl_result["url"]
        assert proc.failCounter == 0
        assert proc.confirmedNew is False
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


class TestProcessFormat:
    def test_txt_found_and_new_sets_confirmed_new(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.txt").write_text("hello")
        monkeypatch.setattr(processor, "verify_content", lambda item, path: True)

        processor.process_format(("txt", a_dir))

        assert processor.confirmedNew is True
        assert processor.failCounter == 0
        assert len(processor.results) == 1
        assert processor.results[0]["format"] == "txt"
        copied = processor.sDir / f"{processor.cTimestamp}.txt"
        assert copied.exists()
        assert processor.fileNames == [copied]

    def test_txt_not_new_increments_fail_counter(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.txt").write_text("hello")
        monkeypatch.setattr(processor, "verify_content", lambda item, path: False)

        processor.process_format(("txt", a_dir))

        assert processor.confirmedNew is False
        assert processor.failCounter == 1
        assert processor.results == []

    def test_missing_file_increments_fail_counter(self, processor, crawl_dirs):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        # No .html file was created for this crawl.

        processor.process_format(("html", a_dir))

        assert processor.failCounter == 1
        assert processor.results == []

    def test_confirmed_new_short_circuits_verify_content(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, _c_dir, a_dir, _j_dir = crawl_dirs
        (a_dir / "capture.html").write_text("<html></html>")
        processor.confirmedNew = True
        calls = []
        monkeypatch.setattr(
            processor, "verify_content", lambda item, path: calls.append(1) or False
        )

        processor.process_format(("html", a_dir))

        assert calls == []
        assert len(processor.results) == 1

    def test_wacz_bypasses_verify_content_entirely(
        self, processor, crawl_dirs, monkeypatch
    ):
        _, c_dir, _a_dir, _j_dir = crawl_dirs
        (c_dir / "archive.wacz").write_bytes(b"fake wacz")
        calls = []
        monkeypatch.setattr(
            processor, "verify_content", lambda item, path: calls.append(1) or False
        )

        processor.process_format(("wacz", c_dir))

        assert calls == []
        assert len(processor.results) == 1
        assert processor.results[0]["format"] == "warc"
        assert processor.results[0]["path"].endswith(".wacz")

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
        monkeypatch.setattr(processor, "verify_content", lambda item, path: True)

        processor.process_format(("warc.json", j_dir))

        assert processor.failCounter == 0
        assert len(processor.results) == 1
        assert processor.results[0]["format"] == "warc.json"
        written = j_dir / f"{processor.cTimestamp}.warc.json"
        assert written.exists()
        copied = processor.sDir / f"{processor.cTimestamp}.warc.json"
        assert copied.exists()


class TestVerifyContent:
    def _item(self):
        return {
            "format": "txt",
            "original_url": "https://example.com/privacy",
            "company_id": "co1",
            "type": "privacy_policy",
        }

    def test_returns_false_when_validation_fails(self, processor, monkeypatch):
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: False)

        assert processor.verify_content(self._item(), Path("dummy.txt")) is False

    def test_returns_true_when_no_previous_capture_mongo(self, processor, monkeypatch):
        CFG.USE_MONGO = True
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)

        assert processor.verify_content(self._item(), Path("dummy.txt")) is True

    def test_compares_against_existing_capture_mongo(self, processor, monkeypatch):
        CFG.USE_MONGO = True
        processor.collection.docs.append(
            {
                "company_id": "co1",
                "type": "privacy_policy",
                "format": "txt",
                "date_fetched": "2024-01-01",
                "path": "co1/privacy_policy/123.txt",
            }
        )
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)
        monkeypatch.setattr(process_module, "compare_files", lambda *a, **k: True)

        assert processor.verify_content(self._item(), Path("dummy.txt")) is True

    def test_uses_local_find_one_when_use_mongo_false(self, processor, monkeypatch):
        CFG.USE_MONGO = False
        processor.collection = [
            {
                "company_id": "co1",
                "type": "privacy_policy",
                "format": "txt",
                "date_fetched": "2024-01-01",
                "path": "co1/privacy_policy/123.txt",
            }
        ]
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)
        monkeypatch.setattr(process_module, "compare_files", lambda *a, **k: False)

        assert processor.verify_content(self._item(), Path("dummy.txt")) is False

    def test_compare_exception_returns_false(self, processor, monkeypatch, caplog):
        CFG.USE_MONGO = True
        processor.collection.docs.append(
            {
                "company_id": "co1",
                "type": "privacy_policy",
                "format": "txt",
                "date_fetched": "2024-01-01",
                "path": "co1/privacy_policy/123.txt",
            }
        )
        monkeypatch.setattr(process_module, "validate_file", lambda path, fmt: True)

        def boom(*a, **k):
            raise RuntimeError("gcs unreachable")

        monkeypatch.setattr(process_module, "compare_files", boom)

        assert processor.verify_content(self._item(), Path("dummy.txt")) is False


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
