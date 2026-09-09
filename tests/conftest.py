# Shared pytest fixtures for the Transparency Archiver test suite.
#
# CFG (source.globals.CFG) is a module-level dataclass singleton imported by
# reference across almost every module in this codebase. That makes it the
# central thing tests must protect against: mutate it in one test and, unless
# something resets it, the next test silently inherits that state. The
# `_cfg_snapshot` fixture below is autouse specifically to close that hole
# without every test having to remember to opt in.

import copy
import dataclasses
import sys

import pytest

from source.globals import CFG, logger
from source.utils import matches_query


@pytest.fixture(autouse=True)
def _cfg_snapshot():
    """Deep-copy CFG before each test and restore every field afterwards."""
    snapshot = copy.deepcopy(CFG)
    yield CFG
    for f in dataclasses.fields(CFG):
        setattr(CFG, f.name, getattr(snapshot, f.name))


@pytest.fixture(autouse=True)
def _logger_handlers():
    """Close/remove any FileHandler a test adds via init_globals().

    Without this, a handler keeps its log file open under a pytest tmp_path
    and Windows will refuse to delete the directory during fixture teardown.
    """
    original = list(logger.handlers)
    yield
    for handler in list(logger.handlers):
        if handler not in original:
            handler.close()
            logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def _clean_argv(monkeypatch):
    """Keep pytest's own argv from leaking into argparse.parse_args() calls."""
    monkeypatch.setattr(sys, "argv", ["transparency-archiver"])


_APP_ENV_VARS = [
    "CLOUD_RUN_EXECUTION",
    "RUNNING_IN_DOCKER",
    "DRY_RUN",
    "TEMP_DIR",
    "CAPTURES_DIR",
    "CHANGES_DIR",
    "LOG_DIR",
    "MONGODB_URL",
    "GCP_BUCKET",
    "GCLOUD_PROJECT",
    "GCLOUD_REGION",
    "GCLOUD_JOB",
    "SYNC_PRIORITY",
    "CONFIG_PATH",
    "SLACK_WEBHOOK_URL",
]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """source.globals loads this repo's real .env at import time (once, for
    the whole process), which can contain real deployment values and
    credentials (a MongoDB Atlas URL, a real GCP bucket/project, etc).
    Strip the specific environment variables this codebase reads so tests
    get deterministic, secret-free defaults no matter what a developer's
    local .env happens to contain."""
    for var in _APP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def cfg_dirs(tmp_path, monkeypatch):
    """Point every CFG directory at a throwaway tree under tmp_path."""
    temp_dir = tmp_path / "tmp"
    docs_dir = temp_dir / "docs"
    captures_dir = tmp_path / "captures"
    log_dir = tmp_path / "logs"

    monkeypatch.setattr(CFG, "TEMP_DIR", temp_dir)
    monkeypatch.setattr(CFG, "CRAWL_DIR", temp_dir)
    monkeypatch.setattr(CFG, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(CFG, "CAPTURES_DIR", captures_dir)
    monkeypatch.setattr(CFG, "LOG_DIR", log_dir)

    for d in (temp_dir, docs_dir, captures_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    return tmp_path


@pytest.fixture
def sample_company():
    return {
        "_id": "675211559efd527760c64413",
        "id": "meta",
        "name": "Meta",
        "doc_urls": {
            "Privacy Policy": {
                "name": "Privacy Policy",
                "urls": ["https://meta.example/privacy"],
            },
            "Terms of Service": {
                "name": "Terms of Service",
                "urls": ["https://meta.example/terms"],
            },
        },
    }


@pytest.fixture
def sample_companies(sample_company):
    return [
        sample_company,
        {
            "_id": "675211559efd527760c6441a",
            "id": "reddit",
            "name": "Reddit",
            "doc_urls": {
                "Privacy Policy": {
                    "name": "Privacy Policy",
                    "urls": ["https://reddit.example/privacy"],
                }
            },
        },
    ]


class FakeCursor:
    """Stands in for a pymongo Cursor: chainable .sort(), then iterable."""

    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_args, **_kwargs):
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeInsertResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class FakeCollection:
    """A minimal stand-in for a pymongo Collection.

    Implements only what this codebase actually calls (find / find_one /
    insert_one / aggregate). It is not a MongoDB emulator: aggregate() just
    hands back whatever canned data a test supplies via `aggregate_result`,
    since replicating real aggregation-pipeline semantics belongs in an
    integration test against a real (or mongomock) server, not here.
    """

    def __init__(self, docs=None, aggregate_result=None):
        self.docs = docs if docs is not None else []
        self._aggregate_result = (
            aggregate_result if aggregate_result is not None else []
        )
        self.aggregate_calls = []
        self.find_calls = []
        self.find_one_calls = []
        self.inserted = []

    def find(self, query=None, sort=None):
        self.find_calls.append((query, sort))
        results = (
            list(self.docs)
            if query is None
            else [d for d in self.docs if matches_query(d, query)]
        )
        if sort:
            for key, direction in reversed(sort):
                results.sort(key=lambda d: d[key], reverse=(direction == -1))
        return FakeCursor(results)

    def find_one(self, query, sort=None):
        self.find_one_calls.append((query, sort))
        results = [d for d in self.docs if matches_query(d, query)]
        if sort:
            for key, direction in reversed(sort):
                results.sort(key=lambda d: d[key], reverse=(direction == -1))
        return results[0] if results else None

    def insert_one(self, doc):
        from bson import ObjectId

        if doc.get("_id") is None:
            doc["_id"] = ObjectId()
        self.docs.append(doc)
        self.inserted.append(doc)
        return FakeInsertResult(doc["_id"])

    def aggregate(self, pipeline):
        self.aggregate_calls.append(pipeline)
        return list(self._aggregate_result)


class FakeMongoClient:
    """databases: {db_name: {collection_name: FakeCollection}}"""

    def __init__(self, databases=None):
        self._databases = databases if databases is not None else {}
        self.closed = False

    def __getitem__(self, db_name):
        return self._databases[db_name]

    def close(self):
        self.closed = True


@pytest.fixture
def fake_collection():
    return FakeCollection()


@pytest.fixture
def fake_mongo_client():
    return FakeMongoClient()
