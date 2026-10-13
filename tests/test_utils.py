# Tests for source/utils.py: the shared I/O helpers (Mongo, GCP, local DB,
# Slack, cleanup) reused across the rest of the capture pipeline.

import json
import logging
from pathlib import Path

import bson
import pytest
from bson import ObjectId

from source.globals import CFG
from source import utils


# --------------------------------------------------------------------------
# join_path
# --------------------------------------------------------------------------


class TestJoinPath:
    def test_joins_and_normalizes(self):
        result = utils.join_path("a", "b", "c")
        assert result == Path("a", "b", "c")

    def test_normalizes_redundant_separators(self):
        result = utils.join_path("a/", "./b", "c")
        assert result == Path("a", "b", "c")


# --------------------------------------------------------------------------
# mongo_connect
# --------------------------------------------------------------------------


class TestMongoConnect:
    def test_raises_when_use_mongo_false(self):
        CFG.USE_MONGO = False
        with pytest.raises(Exception):
            utils.mongo_connect()

    def test_connects_when_use_mongo_true(self, monkeypatch):
        CFG.USE_MONGO = True
        CFG.MONGODB_URL = "mongodb://example/"
        captured = {}

        class FakeClient:
            def __init__(self, url, server_api=None):
                captured["url"] = url
                captured["server_api"] = server_api

        monkeypatch.setattr(utils, "MongoClient", FakeClient)

        client = utils.mongo_connect()

        assert isinstance(client, FakeClient)
        assert captured["url"] == "mongodb://example/"


# --------------------------------------------------------------------------
# get_all_companies
# --------------------------------------------------------------------------


class TestGetAllCompanies:
    def test_uses_local_db_when_use_local_companies(
        self, monkeypatch, sample_companies
    ):
        CFG.USE_MONGO = True
        CFG.USE_LOCAL_COMPANIES = True
        CFG.COMPANIES_JSON = Path("db/companies.json")
        called = {}

        def fake_pull_db(path, create_new):
            called["path"] = path
            called["create_new"] = create_new
            return sample_companies

        monkeypatch.setattr(utils, "pull_db", fake_pull_db)

        result = utils.get_all_companies()

        assert result == sample_companies
        assert called == {"path": CFG.COMPANIES_JSON, "create_new": False}

    def test_uses_local_db_when_use_mongo_false(self, monkeypatch, sample_companies):
        CFG.USE_MONGO = False
        CFG.USE_LOCAL_COMPANIES = False
        monkeypatch.setattr(utils, "pull_db", lambda path, create_new: sample_companies)

        assert utils.get_all_companies() == sample_companies

    def test_fetches_from_mongo_and_closes_client(self, monkeypatch, sample_companies):
        CFG.USE_MONGO = True
        CFG.USE_LOCAL_COMPANIES = False
        CFG.DB_NAME = "dev_db"
        closed = {"value": False}

        class FakeCursorLocal:
            def sort(self, *_a, **_k):
                return sample_companies

        class FakeCollectionLocal:
            def find(self):
                return FakeCursorLocal()

        class FakeClient:
            def __getitem__(self, name):
                return {"companies": FakeCollectionLocal()}

            def close(self):
                closed["value"] = True

        monkeypatch.setattr(utils, "mongo_connect", lambda: FakeClient())

        result = utils.get_all_companies()

        assert result == sample_companies
        assert closed["value"] is True

    def test_mongo_connect_failure_raises_wrapped_exception(self, monkeypatch):
        CFG.USE_MONGO = True
        CFG.USE_LOCAL_COMPANIES = False

        def boom():
            raise RuntimeError("no mongo for you")

        monkeypatch.setattr(utils, "mongo_connect", boom)

        with pytest.raises(Exception, match="Failed to fetch companies from MongoDB"):
            utils.get_all_companies()


# --------------------------------------------------------------------------
# get_validated_companies
# --------------------------------------------------------------------------


class TestGetValidatedCompanies:
    def test_filters_out_invalid_companies(self, monkeypatch, sample_company, caplog):
        invalid = {"name": "No Id", "doc_urls": {}}
        monkeypatch.setattr(
            utils, "get_all_companies", lambda: [sample_company, invalid]
        )
        CFG.USE_LOCAL_COMPANIES = True
        CFG.PRIORITY_DICT = {}

        with caplog.at_level(logging.WARNING):
            result = utils.get_validated_companies()

        assert result == [sample_company]
        assert "Invalid company structure" in caplog.text

    def test_local_companies_skip_priority_filtering(
        self, monkeypatch, sample_companies
    ):
        monkeypatch.setattr(utils, "get_all_companies", lambda: sample_companies)
        CFG.USE_LOCAL_COMPANIES = True
        CFG.PRIORITY_DICT = {sample_companies[0]["_id"]: 0}

        result = utils.get_validated_companies()

        assert result == sample_companies

    def test_priority_zero_excludes_the_company(self, monkeypatch, sample_companies):
        monkeypatch.setattr(utils, "get_all_companies", lambda: sample_companies)
        CFG.USE_LOCAL_COMPANIES = False
        CFG.SYNC_PRIORITY = 1
        zero_priority_id = sample_companies[0]["_id"]
        CFG.PRIORITY_DICT = {zero_priority_id: 0}

        result = utils.get_validated_companies()

        assert [c["_id"] for c in result] == [sample_companies[1]["_id"]]

    def test_sync_priority_filters_below_threshold_and_absent_entries(
        self, monkeypatch, sample_companies
    ):
        """With the default DEFAULT_PRIORITY of 1, a company *not* present
        in PRIORITY_DICT counts as priority 1, so it's excluded whenever
        SYNC_PRIORITY > 1 -- not just companies with an explicit low
        priority."""
        monkeypatch.setattr(utils, "get_all_companies", lambda: sample_companies)
        CFG.USE_LOCAL_COMPANIES = False
        CFG.SYNC_PRIORITY = 3
        high_priority_id = sample_companies[0]["_id"]
        # sample_companies[1] is intentionally absent from PRIORITY_DICT.
        CFG.PRIORITY_DICT = {high_priority_id: 3}

        result = utils.get_validated_companies()

        assert [c["_id"] for c in result] == [high_priority_id]

    def test_empty_priority_dict_skips_filtering_entirely(
        self, monkeypatch, sample_companies
    ):
        monkeypatch.setattr(utils, "get_all_companies", lambda: sample_companies)
        CFG.USE_LOCAL_COMPANIES = False
        CFG.SYNC_PRIORITY = 4
        CFG.PRIORITY_DICT = {}

        result = utils.get_validated_companies()

        assert result == sample_companies


class TestInactiveCompanies:
    @pytest.mark.parametrize("use_local", [False, True])
    def test_inactive_companies_are_never_captured(self, monkeypatch, use_local):
        companies = [
            {
                "_id": cid,
                "id": cid,
                "name": cid,
                "doc_urls": {},
                "metadata": {"status": status},
            }
            for cid, status in (("live", "Active"), ("retired", "Inactive"))
        ]
        monkeypatch.setattr(utils, "get_all_companies", lambda: companies)
        CFG.USE_LOCAL_COMPANIES = use_local
        CFG.SYNC_PRIORITY = 1
        # Even an explicit daily priority doesn't override Inactive
        CFG.PRIORITY_DICT = {"retired": 3}

        result = utils.get_validated_companies()

        assert [c["_id"] for c in result] == ["live"]


class TestCaptureTiers:
    """Daily (3) / weekly (2, the default) / monthly (1) capture tiers, as
    configured in production: DEFAULT_PRIORITY 2, fringe companies listed at
    priority 1 in PRIORITY_DICT."""

    @pytest.fixture
    def tiered(self, monkeypatch):
        companies = [
            {"_id": cid, "id": cid, "name": cid, "doc_urls": {}}
            for cid in ("daily", "weekly", "monthly", "disabled")
        ]
        monkeypatch.setattr(utils, "get_all_companies", lambda: companies)
        CFG.USE_LOCAL_COMPANIES = False
        CFG.DEFAULT_PRIORITY = 2
        CFG.PRIORITY_DICT = {"daily": 3, "monthly": 1, "disabled": 0}

    def _run(self, sync_priority, sync_priority_max=0):
        CFG.SYNC_PRIORITY = sync_priority
        CFG.SYNC_PRIORITY_MAX = sync_priority_max
        return [c["_id"] for c in utils.get_validated_companies()]

    def test_daily_run_only_captures_daily_tier(self, tiered):
        assert self._run(3) == ["daily"]

    def test_weekly_run_captures_default_tier_but_not_monthly(self, tiered):
        assert self._run(2) == ["daily", "weekly"]

    def test_monthly_run_captures_only_the_monthly_tier(self, tiered):
        assert self._run(1, sync_priority_max=1) == ["monthly"]

    def test_full_run_captures_everything_but_disabled(self, tiered):
        assert self._run(1) == ["daily", "weekly", "monthly"]


# --------------------------------------------------------------------------
# format_data
# --------------------------------------------------------------------------


class TestFormatData:
    def _no_shuffle(self, monkeypatch):
        monkeypatch.setattr(utils.random, "shuffle", lambda seq: None)

    def test_basic_single_company(self, monkeypatch, sample_company):
        self._no_shuffle(monkeypatch)
        CFG.RECOVERY_DATA = []

        result = utils.format_data([sample_company])

        expected_groups = {
            tuple(entry["urls"]) for entry in sample_company["doc_urls"].values()
        }
        groups = {tuple(urls) for urls, _ in result}
        assert groups == expected_groups
        for urls, entries in result:
            assert len(entries) == 1
            entry = entries[0]
            assert entry["urls"] == urls
            assert entry["objId"] == str(sample_company["_id"])
            assert entry["name"] == sample_company["name"]
            assert entry["pType"] in {"privacy_policy", "terms_of_service"}

    def test_multiple_urls_per_doc_type_form_one_crawl_group(
        self, monkeypatch, sample_company
    ):
        # A doc type listing more than one url is one logical document split
        # across pages -- it must stay a single crawl unit, not be split into
        # one crawl per url.
        self._no_shuffle(monkeypatch)
        CFG.RECOVERY_DATA = []
        company = {
            **sample_company,
            "doc_urls": {
                "Privacy Policy": {
                    "name": "Privacy Policy",
                    "urls": [
                        "https://meta.example/privacy",
                        "https://meta.example/privacy-alt",
                    ],
                }
            },
        }

        result = utils.format_data([company])

        assert len(result) == 1
        urls, entries = result[0]
        assert urls == [
            "https://meta.example/privacy",
            "https://meta.example/privacy-alt",
        ]
        assert len(entries) == 1
        assert entries[0]["pType"] == "privacy_policy"
        assert entries[0]["urls"] == urls

    def test_multi_url_group_preserves_given_order(self, monkeypatch, sample_company):
        # doc_urls "urls" order reflects how the pages were intended to be
        # read (e.g. page 2 of a policy before page 1) -- format_data must
        # never reorder it. Deliberately not alphabetical, so a stray sort()
        # would be caught.
        self._no_shuffle(monkeypatch)
        CFG.RECOVERY_DATA = []
        ordered_urls = [
            "https://meta.example/z-page",
            "https://meta.example/a-page",
            "https://meta.example/m-page",
        ]
        company = {
            **sample_company,
            "doc_urls": {
                "Privacy Policy": {"name": "Privacy Policy", "urls": ordered_urls}
            },
        }

        result = utils.format_data([company])

        urls, entries = result[0]
        assert urls == ordered_urls
        assert entries[0]["urls"] == ordered_urls

    def test_empty_urls_list_is_skipped(self, monkeypatch, sample_company):
        self._no_shuffle(monkeypatch)
        CFG.RECOVERY_DATA = []
        company = {
            **sample_company,
            "doc_urls": {"Privacy Policy": {"name": "Privacy Policy", "urls": []}},
        }

        result = utils.format_data([company])

        assert result == []

    def test_duplicate_url_groups_are_collated(self, monkeypatch, sample_company):
        self._no_shuffle(monkeypatch)
        CFG.RECOVERY_DATA = []
        shared_urls = ["https://shared.example/policy", "https://shared.example/p2"]
        shared_doc_urls = {
            "Privacy Policy": {"name": "Privacy Policy", "urls": shared_urls}
        }
        company_a = {
            **sample_company,
            "_id": "a1",
            "doc_urls": shared_doc_urls,
        }
        company_b = {
            **sample_company,
            "_id": "b2",
            "name": "B",
            "doc_urls": shared_doc_urls,
        }

        result = utils.format_data([company_a, company_b])

        assert len(result) == 1
        urls, entries = result[0]
        assert urls == shared_urls
        assert {e["objId"] for e in entries} == {"a1", "b2"}

    def test_recovery_data_skips_matching_urls(self, monkeypatch, sample_companies):
        self._no_shuffle(monkeypatch)
        skipped_url = sample_companies[0]["doc_urls"]["Privacy Policy"]["urls"][0]
        CFG.RECOVERY_DATA = [skipped_url]

        result = utils.format_data(sample_companies)

        all_urls = {url for urls, _ in result for url in urls}
        assert skipped_url not in all_urls

    def test_recovery_data_skips_group_if_any_constituent_url_matches(
        self, monkeypatch, sample_company
    ):
        self._no_shuffle(monkeypatch)
        company = {
            **sample_company,
            "doc_urls": {
                "Privacy Policy": {
                    "name": "Privacy Policy",
                    "urls": [
                        "https://meta.example/privacy",
                        "https://meta.example/privacy-alt",
                    ],
                }
            },
        }
        # Only one of the two urls in the group was recorded as captured --
        # the whole group should still be skipped, since it was crawled as
        # a single unit last time.
        CFG.RECOVERY_DATA = ["https://meta.example/privacy-alt"]

        result = utils.format_data([company])

        assert result == []

    def test_calls_shuffle(self, monkeypatch, sample_company):
        CFG.RECOVERY_DATA = []
        calls = []
        monkeypatch.setattr(utils.random, "shuffle", lambda seq: calls.append(seq))

        utils.format_data([sample_company])

        assert len(calls) == 1


# --------------------------------------------------------------------------
# upload_to_GCP
# --------------------------------------------------------------------------


class TestUploadToGCP:
    def test_raises_in_cloud_mode(self):
        CFG.IN_CLOUD = True
        CFG.USE_GCP = True
        with pytest.raises(Exception):
            utils.upload_to_GCP([], object())

    def test_raises_when_gcp_disabled(self):
        CFG.IN_CLOUD = False
        CFG.USE_GCP = False
        with pytest.raises(Exception):
            utils.upload_to_GCP([], object())

    def test_uploads_and_strips_docs_dir_prefix(self, monkeypatch, tmp_path, caplog):
        CFG.IN_CLOUD = False
        CFG.USE_GCP = True
        CFG.DOCS_DIR = tmp_path / "docs"
        CFG.GCP_BUCKET = "my-bucket"
        file_names = [tmp_path / "docs" / "co1" / "privacy" / "1.txt"]
        captured = {}

        def fake_upload(bucket, gcs_file_names, source_directory, max_workers):
            captured["bucket"] = bucket
            captured["names"] = gcs_file_names
            captured["source_directory"] = source_directory
            return [None]  # None == success for each blob

        import google.cloud.storage.transfer_manager as transfer_manager

        monkeypatch.setattr(transfer_manager, "upload_many_from_filenames", fake_upload)

        with caplog.at_level(logging.INFO):
            utils.upload_to_GCP(file_names, "bucket-handle")

        assert captured["names"] == ["co1/privacy/1.txt"]
        assert captured["source_directory"] == str(CFG.DOCS_DIR)
        assert "Uploaded co1/privacy/1.txt to my-bucket." in caplog.text

    def test_logs_per_file_exceptions_without_raising(
        self, monkeypatch, tmp_path, caplog
    ):
        CFG.IN_CLOUD = False
        CFG.USE_GCP = True
        CFG.DOCS_DIR = tmp_path / "docs"
        file_names = [tmp_path / "docs" / "a.txt"]

        import google.cloud.storage.transfer_manager as transfer_manager

        monkeypatch.setattr(
            transfer_manager,
            "upload_many_from_filenames",
            lambda *a, **k: [Exception("network blip")],
        )

        with caplog.at_level(logging.ERROR):
            utils.upload_to_GCP(file_names, "bucket-handle")

        assert "Failed to upload a.txt" in caplog.text

    def test_reraises_unexpected_failure(self, monkeypatch, tmp_path):
        CFG.IN_CLOUD = False
        CFG.USE_GCP = True
        CFG.DOCS_DIR = tmp_path / "docs"

        import google.cloud.storage.transfer_manager as transfer_manager

        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(transfer_manager, "upload_many_from_filenames", boom)

        with pytest.raises(RuntimeError):
            utils.upload_to_GCP([tmp_path / "docs" / "a.txt"], "bucket-handle")


# --------------------------------------------------------------------------
# update_Mongo
# --------------------------------------------------------------------------


class TestUpdateMongo:
    def test_raises_when_use_mongo_false(self):
        CFG.USE_MONGO = False
        with pytest.raises(Exception):
            utils.update_Mongo([{"path": "x"}], fake_collection_stub())

    def test_inserts_all_results(self):
        CFG.USE_MONGO = True
        collection = fake_collection_stub()
        results = [{"path": "a"}, {"path": "b"}]

        utils.update_Mongo(results, collection)

        assert collection.docs == results

    def test_continues_after_a_single_insert_failure(self, caplog):
        CFG.USE_MONGO = True

        class FlakyCollection:
            def __init__(self):
                self.docs = []
                self.calls = 0

            def insert_one(self, doc):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("duplicate key")
                self.docs.append(doc)

        collection = FlakyCollection()
        results = [{"path": "fails"}, {"path": "succeeds"}]

        with caplog.at_level(logging.ERROR):
            utils.update_Mongo(results, collection)

        assert collection.docs == [{"path": "succeeds"}]
        assert "Failed to upload document" in caplog.text


def fake_collection_stub():
    class Stub:
        def __init__(self):
            self.docs = []

        def insert_one(self, doc):
            self.docs.append(doc)

    return Stub()


# --------------------------------------------------------------------------
# get_gcs_bytes / decode_bytes
# --------------------------------------------------------------------------


class TestGetGcsBytes:
    def test_reads_local_file_when_in_cloud(self, tmp_path):
        CFG.IN_CLOUD = True
        CFG.USE_GCP = True
        CFG.CAPTURES_DIR = tmp_path
        (tmp_path / "co1").mkdir()
        target = tmp_path / "co1" / "doc.txt"
        target.write_bytes(b"hello world")

        result = utils.get_gcs_bytes("co1/doc.txt", bucket=None)

        assert result == b"hello world"

    def test_reads_local_file_when_gcp_disabled(self, tmp_path):
        CFG.IN_CLOUD = False
        CFG.USE_GCP = False
        CFG.CAPTURES_DIR = tmp_path
        (tmp_path / "doc.txt").write_bytes(b"offline content")

        result = utils.get_gcs_bytes("doc.txt", bucket=None)

        assert result == b"offline content"

    def test_reads_from_bucket_blob(self):
        CFG.IN_CLOUD = False
        CFG.USE_GCP = True

        class FakeBlob:
            def download_as_bytes(self, timeout):
                assert timeout == 120
                return b"gcs bytes"

        class FakeBucket:
            def blob(self, path):
                assert path == "co1/doc.txt"
                return FakeBlob()

        result = utils.get_gcs_bytes("co1/doc.txt", FakeBucket())

        assert result == b"gcs bytes"


class TestDecodeBytes:
    def test_decodes_utf8(self):
        assert utils.decode_bytes("hello".encode("utf-8")) == "hello"

    def test_falls_back_to_utf8_ignore_when_charset_normalizer_fails(self, monkeypatch):
        class FakeResult:
            def best(self):
                return None

        monkeypatch.setattr(utils, "from_bytes", lambda data: FakeResult())

        result = utils.decode_bytes(b"raw bytes")

        assert result == "raw bytes"


# --------------------------------------------------------------------------
# DocumentCache
# --------------------------------------------------------------------------


class TestDocumentCache:
    def test_get_and_put(self):
        cache = utils.DocumentCache(capacity=2)
        assert cache.get("a") is None
        cache.put("a", "textA")
        assert cache.get("a") == "textA"
        assert len(cache) == 1

    def test_evicts_least_recently_used(self):
        cache = utils.DocumentCache(capacity=2)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.put("c", "3")  # should evict "a"

        assert cache.get("a") is None
        assert cache.get("b") == "2"
        assert cache.get("c") == "3"

    def test_get_marks_most_recently_used(self):
        cache = utils.DocumentCache(capacity=2)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.get("a")  # "a" is now more recent than "b"
        cache.put("c", "3")  # should evict "b", not "a"

        assert cache.get("a") == "1"
        assert cache.get("b") is None
        assert cache.get("c") == "3"

    def test_put_on_existing_key_updates_value_and_recency(self):
        cache = utils.DocumentCache(capacity=2)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.put("a", "1-updated")

        assert len(cache) == 2
        assert cache.get("a") == "1-updated"


# --------------------------------------------------------------------------
# save_db / pull_db
# --------------------------------------------------------------------------


class TestSaveAndPullDb:
    def test_save_db_assigns_object_id_when_missing(self, tmp_path):
        db_path = tmp_path / "documents.bson"
        utils.save_db([{"path": "a"}], db_path)

        with open(db_path, "rb") as f:
            saved = bson.decode_all(f.read())

        assert len(saved) == 1
        assert "_id" in saved[0]
        assert isinstance(saved[0]["_id"], ObjectId)

    def test_save_db_preserves_existing_object_id(self, tmp_path):
        db_path = tmp_path / "documents.bson"
        existing_id = ObjectId()
        utils.save_db([{"_id": existing_id, "path": "a"}], db_path)

        with open(db_path, "rb") as f:
            saved = bson.decode_all(f.read())

        assert saved[0]["_id"] == existing_id

    def test_save_db_appends_across_calls(self, tmp_path):
        db_path = tmp_path / "documents.bson"
        utils.save_db([{"path": "a"}], db_path)
        utils.save_db([{"path": "b"}], db_path)

        with open(db_path, "rb") as f:
            saved = bson.decode_all(f.read())

        assert [d["path"] for d in saved] == ["a", "b"]

    def test_pull_db_reads_json_list(self, tmp_path):
        db_path = tmp_path / "companies.json"
        db_path.write_text(json.dumps([{"id": 1}, {"id": 2}]))

        assert utils.pull_db(db_path) == [{"id": 1}, {"id": 2}]

    def test_pull_db_converts_json_dict_to_list(self, tmp_path):
        db_path = tmp_path / "companies.json"
        db_path.write_text(json.dumps({"a": {"id": 1}, "b": {"id": 2}}))

        result = utils.pull_db(db_path)

        assert sorted(result, key=lambda d: d["id"]) == [{"id": 1}, {"id": 2}]

    def test_pull_db_reads_bson(self, tmp_path):
        db_path = tmp_path / "documents.bson"
        utils.save_db([{"path": "a"}], db_path)

        result = utils.pull_db(db_path)

        assert result[0]["path"] == "a"

    def test_pull_db_missing_file_creates_new_by_default(self, tmp_path):
        assert utils.pull_db(tmp_path / "missing.json") == []

    def test_pull_db_missing_file_raises_when_create_new_false(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            utils.pull_db(tmp_path / "missing.json", createNew=False)

    def test_pull_db_rejects_unknown_extension(self, tmp_path):
        db_path = tmp_path / "companies.txt"
        db_path.write_text("not relevant")

        with pytest.raises(TypeError):
            utils.pull_db(db_path)


# --------------------------------------------------------------------------
# local_find
# --------------------------------------------------------------------------


class TestLocalFind:
    def test_returns_every_match(self):
        db = [{"a": 1, "v": 1}, {"a": 1, "v": 2}, {"a": 2, "v": 3}]
        assert utils.local_find(db, {"a": 1}) == [{"a": 1, "v": 1}, {"a": 1, "v": 2}]

    def test_returns_empty_list_when_no_match(self):
        assert utils.local_find([{"a": 1}], {"a": 2}) == []

    def test_sorts_all_matches(self):
        db = [{"v": 3}, {"v": 1}, {"v": 2}]
        assert utils.local_find(db, {}, sort=[("v", -1)]) == [
            {"v": 3},
            {"v": 2},
            {"v": 1},
        ]

    def test_dotted_key_with_literal_dot_in_field_name_is_not_a_nested_path(self):
        """A doc whose "formats" dict has a key that itself contains a "."
        (e.g. "warc.json") can't be matched via a "formats.warc.json" query
        -- that gets parsed as the nested path formats->warc->json, not the
        single literal key "warc.json". Membership must be checked in
        plain Python instead (`mongoFormat in doc.get("formats", {})`),
        which is exactly why source.process.ProcessCrawl.verify_content
        no longer folds the format-presence check into the query itself."""
        db = [
            {"company_id": "co1", "formats": {"warc.json": {"extension": "warc.json"}}}
        ]

        assert utils.local_find(db, {"formats.warc.json": {"$exists": True}}) == []
        assert [
            d for d in utils.local_find(db, {}) if "warc.json" in d["formats"]
        ] == db


# --------------------------------------------------------------------------
# local_find_one
# --------------------------------------------------------------------------


class TestLocalFindOne:
    def test_matches_all_query_keys(self):
        db = [{"a": 1, "b": 2}, {"a": 1, "b": 3}]
        assert utils.local_find_one(db, {"a": 1, "b": 3}) == {"a": 1, "b": 3}

    def test_returns_none_when_no_match(self):
        db = [{"a": 1}]
        assert utils.local_find_one(db, {"a": 2}) is None

    def test_single_field_sort_descending(self):
        db = [{"v": 1}, {"v": 3}, {"v": 2}]
        assert utils.local_find_one(db, {}, sort=[("v", -1)]) == {"v": 3}

    def test_single_field_sort_ascending(self):
        db = [{"v": 1}, {"v": 3}, {"v": 2}]
        assert utils.local_find_one(db, {}, sort=[("v", 1)]) == {"v": 1}

    def test_multi_field_sort_uses_first_key_as_primary(self):
        db = [
            {"group": "b", "v": 1},
            {"group": "a", "v": 2},
            {"group": "a", "v": 1},
        ]
        result = utils.local_find_one(db, {}, sort=[("group", 1), ("v", 1)])
        assert result == {"group": "a", "v": 1}

    def test_exists_operator_matches_nested_dotted_key(self):
        db = [
            {"company_id": "co1", "formats": {"txt": {"extension": "txt"}}},
            {"company_id": "co2", "formats": {"html": {"extension": "html"}}},
        ]
        result = utils.local_find_one(db, {"formats.txt": {"$exists": True}})
        assert result == db[0]

    def test_exists_false_matches_when_dotted_key_absent(self):
        db = [
            {"company_id": "co1", "formats": {"txt": {"extension": "txt"}}},
            {"company_id": "co2", "formats": {"html": {"extension": "html"}}},
        ]
        result = utils.local_find_one(db, {"formats.txt": {"$exists": False}})
        assert result == db[1]


class TestMatchesQuery:
    def test_dotted_key_traverses_nested_dicts(self):
        assert utils.get_nested({"a": {"b": 1}}, "a.b") == 1

    def test_dotted_key_missing_intermediate_returns_none(self):
        assert utils.get_nested({"a": 1}, "a.b") is None


# --------------------------------------------------------------------------
# delete_temp_gcs
# --------------------------------------------------------------------------


class TestDeleteTempGcs:
    def test_noop_when_not_in_cloud(self, caplog):
        CFG.IN_CLOUD = False
        with caplog.at_level(logging.ERROR):
            utils.delete_temp_gcs("bucket", "prefix")
        assert "cloud-only feature" in caplog.text

    def test_deletes_blobs_when_present(self, monkeypatch):
        CFG.IN_CLOUD = True
        deleted = {}

        class FakeBucket:
            def list_blobs(self, prefix):
                deleted["prefix"] = prefix
                return ["blob1", "blob2"]

            def delete_blobs(self, blobs):
                deleted["blobs"] = blobs

        class FakeClient:
            def get_bucket(self, name):
                deleted["bucket_name"] = name
                return FakeBucket()

        import google.cloud.storage as gcs_storage

        monkeypatch.setattr(gcs_storage, "Client", FakeClient)

        utils.delete_temp_gcs("my-bucket", "tmp/prefix")

        assert deleted == {
            "prefix": "tmp/prefix",
            "blobs": ["blob1", "blob2"],
            "bucket_name": "my-bucket",
        }

    def test_skips_delete_blobs_when_no_blobs_found(self, monkeypatch):
        CFG.IN_CLOUD = True
        calls = {"delete_called": False}

        class FakeBucket:
            def list_blobs(self, prefix):
                return []

            def delete_blobs(self, blobs):
                calls["delete_called"] = True

        class FakeClient:
            def get_bucket(self, name):
                return FakeBucket()

        import google.cloud.storage as gcs_storage

        monkeypatch.setattr(gcs_storage, "Client", FakeClient)

        utils.delete_temp_gcs("my-bucket", "tmp/prefix")

        assert calls["delete_called"] is False


# --------------------------------------------------------------------------
# notify_slack
# --------------------------------------------------------------------------


class TestNotifySlack:
    def test_noop_when_dry_run(self, monkeypatch):
        CFG.DRY_RUN = True
        CFG.USE_SLACK = True
        CFG.SLACK_WEBHOOK_URL = "https://hooks.example/x"
        called = {"value": False}
        monkeypatch.setattr(
            utils.urllib.request, "urlopen", lambda *a, **k: called.update(value=True)
        )

        utils.notify_slack("hello")

        assert called["value"] is False

    def test_noop_when_slack_disabled(self, monkeypatch):
        CFG.DRY_RUN = False
        CFG.USE_SLACK = False
        CFG.SLACK_WEBHOOK_URL = "https://hooks.example/x"
        called = {"value": False}
        monkeypatch.setattr(
            utils.urllib.request, "urlopen", lambda *a, **k: called.update(value=True)
        )

        utils.notify_slack("hello")

        assert called["value"] is False

    def test_noop_when_webhook_url_empty(self, monkeypatch):
        CFG.DRY_RUN = False
        CFG.USE_SLACK = True
        CFG.SLACK_WEBHOOK_URL = ""
        called = {"value": False}
        monkeypatch.setattr(
            utils.urllib.request, "urlopen", lambda *a, **k: called.update(value=True)
        )

        utils.notify_slack("hello")

        assert called["value"] is False

    def test_sends_request_when_enabled(self, monkeypatch):
        CFG.DRY_RUN = False
        CFG.USE_SLACK = True
        CFG.SLACK_WEBHOOK_URL = "https://hooks.example/x"
        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        monkeypatch.setattr(utils.urllib.request, "urlopen", fake_urlopen)

        utils.notify_slack("a new document was found")

        assert captured["url"] == "https://hooks.example/x"
        assert captured["data"] == {"text": "a new document was found"}
        assert captured["timeout"] == 10

    def test_logs_warning_on_non_200_status(self, monkeypatch, caplog):
        CFG.DRY_RUN = False
        CFG.USE_SLACK = True
        CFG.SLACK_WEBHOOK_URL = "https://hooks.example/x"

        class FakeResponse:
            status = 500

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(
            utils.urllib.request, "urlopen", lambda *a, **k: FakeResponse()
        )

        with caplog.at_level(logging.WARNING):
            utils.notify_slack("hello")

        assert "unexpected status" in caplog.text

    def test_swallows_request_exceptions(self, monkeypatch, caplog):
        CFG.DRY_RUN = False
        CFG.USE_SLACK = True
        CFG.SLACK_WEBHOOK_URL = "https://hooks.example/x"

        def boom(*a, **k):
            raise OSError("network unreachable")

        monkeypatch.setattr(utils.urllib.request, "urlopen", boom)

        with caplog.at_level(logging.WARNING):
            utils.notify_slack("hello")  # must not raise

        assert "Slack notification failed" in caplog.text


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------


class TestCleanup:
    def test_removes_recovery_log_and_temp_dir(self, tmp_path, monkeypatch, caplog):
        CFG.IN_CLOUD = False
        CFG.LOG_DIR = tmp_path / "logs"
        CFG.LOG_DIR.mkdir()
        (CFG.LOG_DIR / "recovery_log.txt").write_text("url\n")
        CFG.TEMP_DIR = tmp_path / "tmp"
        CFG.TEMP_DIR.mkdir()
        (CFG.TEMP_DIR / "leftover.txt").write_text("data")
        monkeypatch.setattr(utils.time, "sleep", lambda *_a: None)

        with caplog.at_level(logging.INFO):
            utils.cleanup()

        assert not (CFG.LOG_DIR / "recovery_log.txt").exists()
        assert not CFG.TEMP_DIR.exists()

    def test_missing_recovery_log_logged_but_not_fatal(
        self, tmp_path, monkeypatch, caplog
    ):
        CFG.IN_CLOUD = False
        CFG.LOG_DIR = tmp_path / "logs"
        CFG.LOG_DIR.mkdir()
        CFG.TEMP_DIR = tmp_path / "tmp"
        CFG.TEMP_DIR.mkdir()
        monkeypatch.setattr(utils.time, "sleep", lambda *_a: None)

        with caplog.at_level(logging.ERROR):
            utils.cleanup()  # must not raise

        assert "file not found" in caplog.text

    def test_permission_error_logged_but_not_fatal(self, tmp_path, monkeypatch, caplog):
        CFG.IN_CLOUD = False
        CFG.LOG_DIR = tmp_path / "logs"
        CFG.LOG_DIR.mkdir()
        (CFG.LOG_DIR / "recovery_log.txt").write_text("url\n")
        CFG.TEMP_DIR = tmp_path / "tmp"
        CFG.TEMP_DIR.mkdir()
        monkeypatch.setattr(utils.time, "sleep", lambda *_a: None)

        def raise_permission_error(_path):
            raise PermissionError("locked")

        monkeypatch.setattr(utils.os, "remove", raise_permission_error)

        with caplog.at_level(logging.ERROR):
            utils.cleanup()  # must not raise

        assert "permission denied" in caplog.text

    def test_uses_delete_temp_gcs_when_in_cloud(self, tmp_path, monkeypatch):
        CFG.IN_CLOUD = True
        CFG.GCP_BUCKET = "my-bucket"
        CFG.LOG_DIR = tmp_path / "logs"
        CFG.LOG_DIR.mkdir()
        (CFG.LOG_DIR / "recovery_log.txt").write_text("url\n")
        CFG.TEMP_DIR = Path("/mnt", "runtime", "tmp")
        monkeypatch.setattr(utils.time, "sleep", lambda *_a: None)
        captured = {}
        monkeypatch.setattr(
            utils,
            "delete_temp_gcs",
            lambda bucket, prefix: captured.update(bucket=bucket, prefix=prefix),
        )

        utils.cleanup()

        assert captured == {"bucket": "my-bucket", "prefix": "runtime/tmp"}
