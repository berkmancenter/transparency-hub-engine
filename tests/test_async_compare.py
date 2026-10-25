# Tests for source/async_compare.py: content extraction from each capture
# format, fuzzy-match comparison against a prior capture, and the basic
# data-quality/language checks run before anything is saved.

import gzip
import io
import json
import zipfile
from pathlib import Path

import pytest

from source.globals import CFG
from source import async_compare as ac


def build_zip(html_content=None, warc_content=None, warc_gz_entry=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if html_content is not None:
            zf.writestr("page.html", html_content)
        if warc_content is not None:
            if warc_gz_entry:
                gz_buf = io.BytesIO()
                with gzip.GzipFile(fileobj=gz_buf, mode="wb") as gz:
                    gz.write(warc_content)
                zf.writestr("data.warc.gz", gz_buf.getvalue())
            else:
                zf.writestr("data.warc", warc_content)
    return buf.getvalue()


# --------------------------------------------------------------------------
# has_content_changed
# --------------------------------------------------------------------------


class TestHasContentChanged:
    def test_identical_text_not_changed(self):
        assert (
            ac.has_content_changed("Hello world", "Hello world", threshold=90) is False
        )

    def test_minor_formatting_differences_not_changed(self):
        old = "Hello,   world!!"
        new = "hello world"
        assert ac.has_content_changed(old, new, threshold=85) is False

    def test_substantially_different_text_is_changed(self):
        old = "The quick brown fox jumps over the lazy dog and keeps running for miles."
        new = "Completely unrelated content discussing something else entirely today."
        assert ac.has_content_changed(old, new, threshold=90) is True

    def test_default_threshold_is_not_frozen_at_import_time(self):
        assert ac.has_content_changed.__defaults__ == (None,)

    def test_omitted_threshold_reads_live_cfg_value(self, monkeypatch):
        """`threshold` defaults to `None` and is resolved from the live
        `CFG.FUZZY_THRESHOLD` inside the function body, so mutating
        `CFG.FUZZY_THRESHOLD` after import takes effect on callers that omit
        the `threshold` kwarg. With a ratio of 50, the live CFG value of 1
        means the content is considered unchanged (50 >= 1)."""
        monkeypatch.setattr(CFG, "FUZZY_THRESHOLD", 1)
        monkeypatch.setattr(ac.fuzz, "ratio", lambda a, b: 50)

        result = ac.has_content_changed("old text", "new text")

        assert result is False


# --------------------------------------------------------------------------
# read_text_from_html
# --------------------------------------------------------------------------


class TestReadTextFromHtml:
    def test_strips_script_style_img_and_unwraps_links(self):
        html = b"""
        <html><body>
        <style>.a{color:red}</style>
        <script>console.log('should be removed')</script>
        <img src="x.png">
        <p>Hello <a href="/x">world</a>!</p>
        </body></html>
        """
        text = ac.read_text_from_html(html)
        assert "console.log" not in text
        assert "color:red" not in text
        assert "Hello" in text
        assert "world" in text


# --------------------------------------------------------------------------
# read_wacz_file
# --------------------------------------------------------------------------


class TestReadWaczFile:
    def test_extracts_text_from_html_and_warc_entries_in_wacz_mode(self):
        zip_bytes = build_zip(
            html_content=b"<html><body>Hello from html</body></html>",
            warc_content=b"Hello from warc",
        )

        text = ac.read_wacz_file(zip_bytes, mode="wacz")

        assert "Hello from warc" in text
        assert "Hello from html" in text

    def test_extracts_text_from_gzipped_warc_entry(self):
        zip_bytes = build_zip(warc_content=b"Gzipped warc text", warc_gz_entry=True)

        text = ac.read_wacz_file(zip_bytes, mode="wacz")

        assert "Gzipped warc text" in text

    def test_warc_gz_mode_gzips_the_whole_archive(self):
        inner_zip = build_zip(html_content=b"<p>Inner content</p>")
        outer_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=outer_buf, mode="wb") as gz:
            gz.write(inner_zip)

        text = ac.read_wacz_file(outer_buf.getvalue(), mode="warc.gz")

        assert "Inner content" in text

    def test_default_mode_reads_as_plain_wacz(self):
        zip_bytes = build_zip(
            html_content=b"<p>Should be read via the default mode</p>"
        )

        assert "Should be read via the default mode" in ac.read_wacz_file(zip_bytes)

    def test_corrupt_bytes_return_empty_string_without_raising(self):
        assert ac.read_wacz_file(b"not a zip file", mode="wacz") == ""


# --------------------------------------------------------------------------
# compare_html_files / compare_txt_files
# --------------------------------------------------------------------------


class TestCompareHtmlFiles:
    def test_detects_change(self, tmp_path):
        local_path = tmp_path / "doc.html"
        local_path.write_bytes(b"<p>Original page content here</p>")
        gcs_bytes = b"<p>Completely different replacement content</p>"

        assert ac.compare_html_files(gcs_bytes, local_path) is True

    def test_no_change_for_identical_html(self, tmp_path):
        content = b"<p>Same content on both sides</p>"
        local_path = tmp_path / "doc.html"
        local_path.write_bytes(content)

        assert ac.compare_html_files(content, local_path) is False


class TestCompareTxtFiles:
    def test_detects_change(self, tmp_path):
        local_path = tmp_path / "doc.txt"
        local_path.write_bytes(b"Original text content")

        assert (
            ac.compare_txt_files(b"Completely different text content", local_path)
            is True
        )

    def test_no_change_for_identical_text(self, tmp_path):
        local_path = tmp_path / "doc.txt"
        local_path.write_bytes(b"identical text content")

        assert ac.compare_txt_files(b"identical text content", local_path) is False


class TestComparePdfFiles:
    def test_uses_extracted_text_for_comparison(self, tmp_path, monkeypatch):
        local_path = tmp_path / "doc.pdf"
        local_path.write_bytes(b"fake pdf bytes")

        class FakePage:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        class FakeReader:
            def __init__(self, source):
                self.source = source

            @property
            def pages(self):
                if isinstance(self.source, io.BytesIO):
                    return [FakePage("gcs pdf text content here")]
                return [FakePage("local pdf text content here")]

        monkeypatch.setattr(ac.PyPDF2, "PdfReader", FakeReader)

        assert ac.compare_pdf_files(b"gcs-bytes", local_path) is True

    def test_identical_extracted_text_not_changed(self, tmp_path, monkeypatch):
        local_path = tmp_path / "doc.pdf"
        local_path.write_bytes(b"fake pdf bytes")

        class FakePage:
            def extract_text(self):
                return "same pdf text on both sides"

        class FakeReader:
            def __init__(self, source):
                pass

            @property
            def pages(self):
                return [FakePage()]

        monkeypatch.setattr(ac.PyPDF2, "PdfReader", FakeReader)

        assert ac.compare_pdf_files(b"gcs-bytes", local_path) is False


class TestCompareJsonFiles:
    def test_compares_extracted_record_content(self, tmp_path):
        local_path = tmp_path / "doc.warc.json"
        local_data = {
            "records": [{"content": "Local content here for testing purposes today"}]
        }
        local_path.write_text(json.dumps(local_data))
        gcs_data = {
            "records": [{"content": "Local content here for testing purposes today"}]
        }

        assert ac.compare_json_files(json.dumps(gcs_data).encode(), local_path) is False

    def test_detects_changed_record_content(self, tmp_path):
        local_path = tmp_path / "doc.warc.json"
        local_data = {
            "records": [{"content": "Original record content goes here today"}]
        }
        local_path.write_text(json.dumps(local_data))
        gcs_data = {
            "records": [{"content": "A totally different record body appears here"}]
        }

        assert ac.compare_json_files(json.dumps(gcs_data).encode(), local_path) is True


# --------------------------------------------------------------------------
# data_quality_check
# --------------------------------------------------------------------------


class TestDataQualityCheck:
    def test_none_or_empty_text_is_invalid(self):
        assert ac.data_quality_check(None) is False
        assert ac.data_quality_check("   ") is False

    def test_too_few_words_is_invalid(self):
        assert ac.data_quality_check("only four words here") is False

    def test_valid_english_text(self, monkeypatch):
        monkeypatch.setattr(ac.langdetect, "detect", lambda text: "en")
        text = " ".join(["word"] * 25)

        assert ac.data_quality_check(text) is True

    def test_non_english_text_is_invalid(self, monkeypatch):
        monkeypatch.setattr(ac.langdetect, "detect", lambda text: "fr")
        text = " ".join(["mot"] * 25)

        assert ac.data_quality_check(text) is False

    def test_langdetect_exception_defaults_to_valid(self, monkeypatch):
        def boom(text):
            raise ac.langdetect.lang_detect_exception.LangDetectException(
                0, "no features"
            )

        monkeypatch.setattr(ac.langdetect, "detect", boom)
        text = " ".join(["word"] * 25)

        assert ac.data_quality_check(text) is True

    def test_generic_exception_defaults_to_valid(self, monkeypatch):
        def boom(text):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(ac.langdetect, "detect", boom)
        text = " ".join(["word"] * 25)

        assert ac.data_quality_check(text) is True


# --------------------------------------------------------------------------
# compare_files / _compare_files_sync
# --------------------------------------------------------------------------


class TestCompareFilesDispatch:
    def test_warc_format_short_circuits_without_fetching_gcs(self, monkeypatch):
        called = {"value": False}
        monkeypatch.setattr(
            ac, "get_gcs_bytes", lambda *a, **k: called.update(value=True) or b""
        )

        assert ac.compare_files(Path("x"), "gcs/path", "warc", bucket=None) is True
        assert called["value"] is False

    @pytest.mark.parametrize("fmt", ["html", "txt", "pdf", "warc.json"])
    def test_dispatches_to_correct_comparator(self, monkeypatch, fmt):
        monkeypatch.setattr(ac, "get_gcs_bytes", lambda *a, **k: b"gcs-bytes")
        seen = {}
        for name in (
            "compare_html_files",
            "compare_txt_files",
            "compare_pdf_files",
            "compare_json_files",
        ):
            monkeypatch.setattr(
                ac,
                name,
                (lambda label: lambda gcs, local: seen.setdefault("fn", label))(name),
            )

        ac.compare_files(Path("x"), "gcs/path", fmt, bucket=None)

        expected = {
            "html": "compare_html_files",
            "txt": "compare_txt_files",
            "pdf": "compare_pdf_files",
            "warc.json": "compare_json_files",
        }[fmt]
        assert seen["fn"] == expected

    def test_unknown_format_still_fetches_gcs_but_returns_false(self, monkeypatch):
        fetched = {"value": False}
        monkeypatch.setattr(
            ac, "get_gcs_bytes", lambda *a, **k: fetched.update(value=True) or b""
        )

        result = ac.compare_files(Path("x"), "gcs/path", "unknown-format", bucket=None)

        assert result is False
        assert fetched["value"] is True

    def test_timeout_returns_false(self, monkeypatch):
        def slow(local_path, gcp_path, format, bucket):
            import time as t

            t.sleep(0.3)
            return True

        monkeypatch.setattr(ac, "_compare_files_sync", slow)

        result = ac.compare_files(
            Path("x"), "gcs/path", "txt", bucket=None, timeout=0.05
        )

        assert result is False


class TestValidateFilesDispatch:
    def test_warc_format_returns_true_without_reading_file(self):
        assert ac.validate_file(Path("nonexistent.file"), "warc") is True

    def test_html_format_reads_and_checks_quality(self, tmp_path, monkeypatch):
        path = tmp_path / "doc.html"
        path.write_bytes(b"<html><body>hi</body></html>")
        monkeypatch.setattr(ac, "data_quality_check", lambda text: True)

        assert ac.validate_file(path, "html") is True

    def test_html_format_never_runs_policy_classifier(self, tmp_path, monkeypatch):
        path = tmp_path / "doc.html"
        path.write_bytes(b"<html><body>hi</body></html>")
        monkeypatch.setattr(ac, "data_quality_check", lambda text: True)
        called = {"value": False}
        monkeypatch.setattr(
            ac,
            "passes_policy_document_check",
            lambda text: called.update(value=True) or False,
        )

        assert ac.validate_file(path, "html") is True
        assert called["value"] is False

    def test_txt_format_fails_when_quality_check_fails(self, tmp_path, monkeypatch):
        path = tmp_path / "doc.txt"
        path.write_bytes(b"too short")
        monkeypatch.setattr(ac, "data_quality_check", lambda text: False)
        called = {"value": False}
        monkeypatch.setattr(
            ac,
            "passes_policy_document_check",
            lambda text: called.update(value=True) or True,
        )

        assert ac.validate_file(path, "txt") is False
        # Classifier check should be skipped entirely once quality check fails
        assert called["value"] is False

    def test_txt_format_defers_to_policy_classifier_check(self, tmp_path, monkeypatch):
        path = tmp_path / "doc.txt"
        path.write_bytes(b"some perfectly fine looking text content")
        monkeypatch.setattr(ac, "data_quality_check", lambda text: True)
        monkeypatch.setattr(ac, "passes_policy_document_check", lambda text: False)

        assert ac.validate_file(path, "txt") is False

    def test_warc_json_format_defers_to_policy_classifier_check(
        self, tmp_path, monkeypatch
    ):
        # warc.json is the crawler's own extracted-text fallback used when a
        # site stops yielding real txt/html/pdf captures (e.g. a bot-check
        # interstitial). It must be gated by the same classifier as txt, or
        # interstitial noise (a fresh Ray ID on every request) gets treated
        # as a real content change on every sync.
        path = tmp_path / "doc.warc.json"
        path.write_text(
            json.dumps({"records": [{"content": "some perfectly fine text"}]})
        )
        monkeypatch.setattr(ac, "data_quality_check", lambda text: True)
        monkeypatch.setattr(ac, "passes_policy_document_check", lambda text: False)

        assert ac.validate_file(path, "warc.json") is False

    def test_timeout_returns_false(self, monkeypatch):
        def slow(local_path, format):
            import time as t

            t.sleep(0.3)
            return True

        monkeypatch.setattr(ac, "_validate_file_sync", slow)

        result = ac.validate_file(Path("x"), "txt", timeout=0.05)

        assert result is False


# --------------------------------------------------------------------------
# passes_policy_document_check
# --------------------------------------------------------------------------


class TestPassesPolicyDocumentCheck:
    def test_true_when_classifier_disabled(self, monkeypatch):
        monkeypatch.setattr(CFG, "USE_POLICY_CLASSIFIER", False)

        assert ac.passes_policy_document_check("irrelevant text") is True

    def test_defers_to_is_policy_document(self, monkeypatch):
        monkeypatch.setattr(CFG, "USE_POLICY_CLASSIFIER", True)
        monkeypatch.setattr(CFG, "POLICY_CLASSIFIER_THRESHOLD", 0.42)
        calls = []

        import source.policy_classifier as pc

        def fake_is_policy_document(text, threshold=0.5, model_path=None):
            calls.append((text, threshold))
            return True

        monkeypatch.setattr(pc, "is_policy_document", fake_is_policy_document)

        assert ac.passes_policy_document_check("some policy text") is True
        assert calls == [("some policy text", 0.42)]

    def test_rejects_when_classifier_scores_below_threshold(self, monkeypatch):
        monkeypatch.setattr(CFG, "USE_POLICY_CLASSIFIER", True)
        import source.policy_classifier as pc

        monkeypatch.setattr(pc, "is_policy_document", lambda *a, **k: False)

        assert ac.passes_policy_document_check("cookie wall text") is False

    def test_fails_open_when_classifier_raises(self, monkeypatch, caplog):
        monkeypatch.setattr(CFG, "USE_POLICY_CLASSIFIER", True)
        import source.policy_classifier as pc

        def boom(*a, **k):
            raise FileNotFoundError("no model on disk")

        monkeypatch.setattr(pc, "is_policy_document", boom)

        assert ac.passes_policy_document_check("some text") is True
        assert "allowing capture through" in caplog.text


# --------------------------------------------------------------------------
# reconstruct_text_from_format and its per-format helpers
# --------------------------------------------------------------------------


class TestReconstructTextFromFormat:
    def test_html_delegates_to_read_text_from_html(self):
        html = b"<html><body><p>Hello reconstructed world</p></body></html>"

        text = ac.reconstruct_text_from_format(html, "html", "html")

        assert "Hello reconstructed world" in text

    def test_pdf_extracts_text_from_all_pages(self, monkeypatch):
        class FakePage:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        class FakeReader:
            def __init__(self, source):
                pass

            @property
            def pages(self):
                return [FakePage("page one "), FakePage("page two")]

        monkeypatch.setattr(ac.PyPDF2, "PdfReader", FakeReader)

        text = ac.reconstruct_text_from_format(b"fake pdf bytes", "pdf", "pdf")

        assert text == "page one page two"

    def test_warc_json_prefers_record_matching_original_url(self):
        raw = json.dumps(
            {
                "records": [
                    {"urls": ["https://a.example/other"], "content": "other page"},
                    {
                        "urls": ["https://a.example/target"],
                        "content": "target page content",
                    },
                ]
            }
        ).encode()

        text = ac.reconstruct_text_from_format(
            raw, "warc.json", "warc.json", originalUrl="https://a.example/target"
        )

        assert text == "target page content"

    def test_warc_json_falls_back_to_joining_all_records(self):
        raw = json.dumps(
            {
                "records": [
                    {"urls": ["https://a.example/1"], "content": "first"},
                    {"urls": ["https://a.example/2"], "content": "second"},
                ]
            }
        ).encode()

        text = ac.reconstruct_text_from_format(
            raw, "warc.json", "warc.json", originalUrl="https://no-match.example"
        )

        assert text == "first\n\nsecond"

    def test_warc_json_matches_any_url_in_a_multi_url_list(self):
        # A document captured from more than one seed url (a page split
        # across multiple urls) should pull in every record that matches any
        # of them, not just the first.
        raw = json.dumps(
            {
                "records": [
                    {"urls": ["https://a.example/other"], "content": "other page"},
                    {"urls": ["https://a.example/1"], "content": "page one"},
                    {"urls": ["https://a.example/2"], "content": "page two"},
                ]
            }
        ).encode()

        text = ac.reconstruct_text_from_format(
            raw,
            "warc.json",
            "warc.json",
            originalUrl=["https://a.example/1", "https://a.example/2"],
        )

        assert text == "page one\n\npage two"

    def test_warc_json_multi_url_records_join_in_seed_url_order(self):
        # Records arrive in whatever order the crawler finished pages, which
        # varies run to run -- the joined text must follow the seed url list
        # instead, or identical captures diff as changed.
        raw = json.dumps(
            {
                "records": [
                    {"urls": ["https://a.example/2/"], "content": "page two"},
                    {"urls": ["https://a.example/1"], "content": "page one"},
                ]
            }
        ).encode()

        text = ac.reconstruct_text_from_format(
            raw,
            "warc.json",
            "warc.json",
            originalUrl=["https://a.example/1", "https://a.example/2"],
        )

        assert text == "page one\n\npage two"

    def test_warc_json_falls_back_to_joining_all_records_when_no_url_in_list_matches(
        self,
    ):
        raw = json.dumps(
            {
                "records": [
                    {"urls": ["https://a.example/1"], "content": "first"},
                    {"urls": ["https://a.example/2"], "content": "second"},
                ]
            }
        ).encode()

        text = ac.reconstruct_text_from_format(
            raw,
            "warc.json",
            "warc.json",
            originalUrl=["https://no-match.example", "https://also-no-match.example"],
        )

        assert text == "first\n\nsecond"

    def test_warc_dispatches_to_read_wacz_file_with_wacz_mode_for_wacz_extension(
        self, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            ac,
            "read_wacz_file",
            lambda data, mode: calls.append((data, mode)) or "extracted",
        )

        result = ac.reconstruct_text_from_format(b"zip-bytes", "warc", "wacz")

        assert result == "extracted"
        assert calls == [(b"zip-bytes", "wacz")]

    def test_warc_dispatches_warc_gz_mode_for_warc_gz_extension(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            ac,
            "read_wacz_file",
            lambda data, mode: calls.append((data, mode)) or "extracted",
        )

        ac.reconstruct_text_from_format(b"zip-bytes", "warc", "warc.gz")

        assert calls == [(b"zip-bytes", "warc.gz")]

    def test_unknown_format_returns_empty_string(self):
        assert ac.reconstruct_text_from_format(b"bytes", "unknown", "ext") == ""

    def test_reconstruction_format_priority_order(self):
        assert ac.RECONSTRUCTION_FORMAT_PRIORITY == ["html", "pdf", "warc.json", "warc"]
