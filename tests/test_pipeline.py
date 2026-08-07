# Tests for source/pipeline.py: DocumentPipeline, which fans a batch of
# URLs out across a thread pool, running the crawl -> process -> store ->
# notify sequence for each one.

from pathlib import Path


from source.globals import CFG
from source import pipeline as pipeline_module
from source.pipeline import DocumentPipeline


def make_pipeline(
    crawler=None,
    collection=None,
    google_bucket=None,
    google_client=None,
    mongo_client=None,
):
    """Build a DocumentPipeline without running its I/O-heavy __init__."""
    pipeline = object.__new__(DocumentPipeline)
    pipeline.crawler = crawler  # type: ignore[assignment]
    pipeline.collection = collection if collection is not None else []
    pipeline.googleBucket = google_bucket
    pipeline.googleClient = google_client
    pipeline.mongoClient = mongo_client  # type: ignore[assignment]
    pipeline.maxWorkers = CFG.MAX_WORKERS
    pipeline.companyData = []
    return pipeline


class FakeCrawler:
    def __init__(self, results_by_url=None, default=None):
        self.results_by_url = results_by_url or {}
        self.default = default if default is not None else []
        self.calls = []

    def run_single_crawl(self, p_data, batch_id):
        self.calls.append((p_data, batch_id))
        return self.results_by_url.get(p_data["url"], self.default)


class FakeProcessCrawl:
    """Swap in for source.pipeline.ProcessCrawl to isolate process_document
    from the real file-processing pipeline."""

    return_value = ([], [])

    def __init__(self, *_args, **_kwargs):
        pass

    def process_crawl(self):
        return type(self).return_value


# --------------------------------------------------------------------------
# __init__
# --------------------------------------------------------------------------


class TestInit:
    def test_mongo_and_gcp_connections_established(
        self, monkeypatch, fake_mongo_client, fake_collection
    ):
        CFG.USE_MONGO = True
        CFG.IN_CLOUD = False
        CFG.USE_GCP = True
        CFG.DB_NAME = "dev_db"
        CFG.GCP_BUCKET = "my-bucket"
        fake_mongo_client._databases["dev_db"] = {"documents_v2": fake_collection}
        monkeypatch.setattr(pipeline_module, "mongo_connect", lambda: fake_mongo_client)

        class FakeGcsClient:
            def bucket(self, name):
                return f"bucket:{name}"

        fake_gcs_client = FakeGcsClient()
        import google.cloud.storage as gcs_storage

        monkeypatch.setattr(gcs_storage, "Client", lambda: fake_gcs_client)

        pipeline = DocumentPipeline([])

        assert pipeline.collection is fake_collection
        assert pipeline.googleClient is fake_gcs_client
        assert pipeline.googleBucket == "bucket:my-bucket"
        assert pipeline.maxWorkers == CFG.MAX_WORKERS

    def test_local_db_used_when_use_mongo_false(self, monkeypatch):
        CFG.USE_MONGO = False
        CFG.IN_CLOUD = False
        CFG.USE_GCP = False
        docs = [{"path": "a"}]
        monkeypatch.setattr(pipeline_module, "pull_db", lambda path: docs)

        pipeline = DocumentPipeline([])

        assert pipeline.collection == docs
        assert pipeline.googleClient is None
        assert pipeline.googleBucket is None

    def test_gcp_client_not_created_in_cloud_mode(self, monkeypatch):
        CFG.USE_MONGO = False
        CFG.IN_CLOUD = True
        CFG.USE_GCP = True
        monkeypatch.setattr(pipeline_module, "pull_db", lambda path: [])
        calls = {"value": 0}

        import google.cloud.storage as gcs_storage

        def fake_client():
            calls["value"] += 1
            return object()

        monkeypatch.setattr(gcs_storage, "Client", fake_client)

        pipeline = DocumentPipeline([])

        assert calls["value"] == 0
        assert pipeline.googleClient is None


# --------------------------------------------------------------------------
# process_document
# --------------------------------------------------------------------------


class TestProcessDocument:
    def test_no_successful_crawl_returns_none(self):
        crawler = FakeCrawler(
            default=[{"success": False, "url": "https://x", "pValues": []}]
        )
        pipeline = make_pipeline(crawler=crawler)
        company_data = {"url": "https://x", "pValues": []}

        assert pipeline.process_document(company_data, "batch_1") is None

    def test_successful_crawl_returns_url_and_company_id(self, monkeypatch):
        crawl_result = {
            "success": True,
            "url": "https://x",
            "objId": "co1",
            "pType": "privacy_policy",
        }
        crawler = FakeCrawler(default=[crawl_result])
        pipeline = make_pipeline(crawler=crawler, collection=[])
        CFG.DRY_RUN = True
        monkeypatch.setattr(pipeline_module, "ProcessCrawl", FakeProcessCrawl)
        company_data = {
            "url": "https://x",
            "pValues": [{"objId": "co1", "name": "X", "pType": "privacy_policy"}],
        }

        result = pipeline.process_document(company_data, "batch_1")

        assert result == ("https://x", "co1")

    def test_dry_run_skips_all_side_effects(self, monkeypatch):
        CFG.DRY_RUN = True
        crawl_result = {
            "success": True,
            "url": "https://x",
            "objId": "co1",
            "pType": "privacy_policy",
        }
        crawler = FakeCrawler(default=[crawl_result])
        pipeline = make_pipeline(crawler=crawler, collection=[])
        FakeProcessCrawl.return_value = (
            [{"formats": {"txt": {}}, "company_id": "co1"}],
            [Path("f.txt")],
        )
        monkeypatch.setattr(pipeline_module, "ProcessCrawl", FakeProcessCrawl)

        called = {
            "update_mongo": False,
            "save_db": False,
            "upload": False,
            "slack": False,
        }
        monkeypatch.setattr(
            pipeline_module,
            "update_Mongo",
            lambda *a, **k: called.update(update_mongo=True),
        )
        monkeypatch.setattr(
            pipeline_module, "save_db", lambda *a, **k: called.update(save_db=True)
        )
        monkeypatch.setattr(
            pipeline_module, "upload_to_GCP", lambda *a, **k: called.update(upload=True)
        )
        monkeypatch.setattr(
            pipeline_module, "notify_slack", lambda *a, **k: called.update(slack=True)
        )

        company_data = {
            "url": "https://x",
            "pValues": [{"objId": "co1", "name": "X", "pType": "privacy_policy"}],
        }
        pipeline.process_document(company_data, "batch_1")

        assert not any(called.values())

    def test_not_dry_run_use_mongo_updates_mongo_and_uploads(self, monkeypatch):
        CFG.DRY_RUN = False
        CFG.USE_MONGO = True
        CFG.IN_CLOUD = False
        crawl_result = {
            "success": True,
            "url": "https://x",
            "objId": "co1",
            "pType": "privacy_policy",
        }
        crawler = FakeCrawler(default=[crawl_result])
        pipeline = make_pipeline(crawler=crawler, collection=[])
        mongo_data = [{"formats": {"txt": {}}, "company_id": "co1"}]
        file_names = [Path("f.txt")]
        FakeProcessCrawl.return_value = (mongo_data, file_names)
        monkeypatch.setattr(pipeline_module, "ProcessCrawl", FakeProcessCrawl)

        captured = {}
        monkeypatch.setattr(
            pipeline_module,
            "update_Mongo",
            lambda data, coll: captured.update(mongo_data=data),
        )
        monkeypatch.setattr(
            pipeline_module,
            "upload_to_GCP",
            lambda names, bucket: captured.update(file_names=names),
        )
        monkeypatch.setattr(
            pipeline_module,
            "notify_slack",
            lambda msg: captured.setdefault("slack", []).append(msg),
        )

        company_data = {
            "url": "https://x",
            "pValues": [{"objId": "co1", "name": "X", "pType": "privacy_policy"}],
        }
        pipeline.process_document(company_data, "batch_1")

        assert captured["mongo_data"] == mongo_data
        assert captured["file_names"] == file_names
        assert captured["slack"] == [
            "New document version detected for X: privacy_policy (https://x)"
        ]

    def test_not_dry_run_use_mongo_false_saves_local_db(self, monkeypatch):
        CFG.DRY_RUN = False
        CFG.USE_MONGO = False
        CFG.IN_CLOUD = False
        CFG.DOCUMENTS_BSON = Path("db/documents.bson")
        crawl_result = {
            "success": True,
            "url": "https://x",
            "objId": "co1",
            "pType": "privacy_policy",
        }
        crawler = FakeCrawler(default=[crawl_result])
        pipeline = make_pipeline(crawler=crawler, collection=[])
        FakeProcessCrawl.return_value = ([], [])
        monkeypatch.setattr(pipeline_module, "ProcessCrawl", FakeProcessCrawl)
        captured = {}
        monkeypatch.setattr(
            pipeline_module,
            "save_db",
            lambda data, path: captured.update(data=data, path=path),
        )
        monkeypatch.setattr(pipeline_module, "upload_to_GCP", lambda *a, **k: None)

        company_data = {"url": "https://x", "pValues": []}
        pipeline.process_document(company_data, "batch_1")

        assert captured["path"] == CFG.DOCUMENTS_BSON

    def test_in_cloud_skips_gcp_upload(self, monkeypatch):
        CFG.DRY_RUN = False
        CFG.USE_MONGO = True
        CFG.IN_CLOUD = True
        crawl_result = {
            "success": True,
            "url": "https://x",
            "objId": "co1",
            "pType": "privacy_policy",
        }
        crawler = FakeCrawler(default=[crawl_result])
        pipeline = make_pipeline(crawler=crawler, collection=[])
        FakeProcessCrawl.return_value = ([], [])
        monkeypatch.setattr(pipeline_module, "ProcessCrawl", FakeProcessCrawl)
        monkeypatch.setattr(pipeline_module, "update_Mongo", lambda *a, **k: None)
        called = {"value": False}
        monkeypatch.setattr(
            pipeline_module, "upload_to_GCP", lambda *a, **k: called.update(value=True)
        )

        company_data = {"url": "https://x", "pValues": []}
        pipeline.process_document(company_data, "batch_1")

        assert called["value"] is False

    def test_slack_only_notified_for_new_txt_ids(self, monkeypatch):
        CFG.DRY_RUN = False
        CFG.USE_MONGO = True
        CFG.IN_CLOUD = True
        crawl_result = {
            "success": True,
            "url": "https://x",
            "objId": "co1",
            "pType": "privacy_policy",
        }
        crawler = FakeCrawler(default=[crawl_result])
        pipeline = make_pipeline(crawler=crawler, collection=[])
        mongo_data = [
            {"formats": {"txt": {}}, "company_id": "co1"},
            {"formats": {"warc": {}}, "company_id": "co2"},
        ]
        FakeProcessCrawl.return_value = (mongo_data, [])
        monkeypatch.setattr(pipeline_module, "ProcessCrawl", FakeProcessCrawl)
        monkeypatch.setattr(pipeline_module, "update_Mongo", lambda *a, **k: None)
        messages = []
        monkeypatch.setattr(
            pipeline_module, "notify_slack", lambda msg: messages.append(msg)
        )

        company_data = {
            "url": "https://x",
            "pValues": [
                {"objId": "co1", "name": "Co1", "pType": "privacy_policy"},
                {"objId": "co2", "name": "Co2", "pType": "terms"},
            ],
        }
        pipeline.process_document(company_data, "batch_1")

        assert messages == [
            "New document version detected for Co1: privacy_policy (https://x)"
        ]


# --------------------------------------------------------------------------
# process_all_documents
# --------------------------------------------------------------------------


class TestProcessAllDocuments:
    def test_writes_recovery_log_only_for_truthy_results(self, tmp_path):
        CFG.LOG_DIR = tmp_path
        CFG.IN_CLOUD = False
        pipeline = make_pipeline(collection=[])
        pipeline.companyData = [
            ("https://a", [{"objId": "co-a"}]),
            ("https://b", [{"objId": "co-b"}]),
        ]

        def fake_process_document(companyData, batch_id):
            if companyData["url"] == "https://a":
                return ("https://a", "co-a")
            return None

        pipeline.process_document = fake_process_document

        results = pipeline.process_all_documents()

        assert results == {"co-a"}
        assert (tmp_path / "recovery_log.txt").read_text() == "https://a\n"

    def test_exception_in_one_future_does_not_abort_others(self, tmp_path, caplog):
        CFG.LOG_DIR = tmp_path
        CFG.IN_CLOUD = False
        pipeline = make_pipeline(collection=[])
        pipeline.companyData = [
            ("https://a", [{"objId": "co-a"}]),
            ("https://b", [{"objId": "co-b"}]),
        ]

        def fake_process_document(companyData, batch_id):
            if companyData["url"] == "https://a":
                raise RuntimeError("boom")
            return ("https://b", "co-b")

        pipeline.process_document = fake_process_document

        results = pipeline.process_all_documents()

        assert results == {"co-b"}
        assert "generated exception" in caplog.text

    def test_closes_google_client_when_not_in_cloud(self, tmp_path):
        CFG.LOG_DIR = tmp_path
        CFG.IN_CLOUD = False

        class FakeGoogleClient:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        google_client = FakeGoogleClient()
        pipeline = make_pipeline(collection=[], google_client=google_client)

        pipeline.process_all_documents()

        assert google_client.closed is True

    def test_closes_mongo_client(self, tmp_path, fake_mongo_client):
        CFG.LOG_DIR = tmp_path
        CFG.IN_CLOUD = False
        pipeline = make_pipeline(collection=[], mongo_client=fake_mongo_client)

        pipeline.process_all_documents()

        assert fake_mongo_client.closed is True
