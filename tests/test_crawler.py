# Tests for source/crawler.py: BrowsertrixCrawler, which builds the crawl
# CLI command and dispatches it to subprocess (local), the Docker SDK
# (docker mode), or Google Cloud Run Jobs (cloud mode).

import subprocess

import docker
import pytest

from source.globals import CFG
from source.crawler import BrowsertrixCrawler


@pytest.fixture
def p_data():
    return {
        "url": "https://example.com/privacy",
        "pValues": [
            {"objId": "co1", "name": "Example Co", "pType": "privacy_policy"},
        ],
    }


class TestConstructor:
    def test_defaults(self):
        crawler = BrowsertrixCrawler()
        assert crawler.dockerImage == "browsertrix-thub:1.2"
        assert crawler.mode == "document_capture"

    def test_custom_values(self):
        crawler = BrowsertrixCrawler(dockerImage="custom:1.0", mode="other")
        assert crawler.dockerImage == "custom:1.0"
        assert crawler.mode == "other"


class TestCreateCrawlCommand:
    def test_local_mode_wraps_in_docker_run(self, monkeypatch, tmp_path):
        CFG.IN_LOCAL = True
        CFG.CRAWL_DIR = tmp_path
        monkeypatch.setattr("source.crawler.time.time", lambda: 1700000000)
        crawler = BrowsertrixCrawler(dockerImage="browsertrix-thub:1.2")

        cmd = crawler.create_crawl_command("https://example.com", "crawl1")

        assert cmd[0:2] == ["docker", "run"]
        assert "--rm" in cmd
        assert "-v" in cmd
        mount_idx = cmd.index("-v") + 1
        assert cmd[mount_idx].endswith(":/crawls")
        assert "--name" in cmd
        name_idx = cmd.index("--name") + 1
        assert cmd[name_idx] == "crawl-crawl1-1700000000"
        assert "browsertrix-thub:1.2" in cmd
        assert "--url" in cmd
        assert cmd[cmd.index("--url") + 1] == "https://example.com"
        assert "--collection" in cmd
        assert cmd[cmd.index("--collection") + 1] == "crawl1"

    def test_non_local_mode_skips_docker_run_wrapper(self):
        CFG.IN_LOCAL = False
        crawler = BrowsertrixCrawler()

        cmd = crawler.create_crawl_command("https://example.com", "crawl1")

        assert cmd[0] == "crawl"
        assert "docker" not in cmd

    def test_document_capture_mode_adds_extra_flags(self):
        CFG.IN_LOCAL = False
        crawler = BrowsertrixCrawler(mode="document_capture")

        cmd = crawler.create_crawl_command("https://example.com", "crawl1")

        assert "--generateWACZ" in cmd
        assert "--generatePDF" in cmd
        assert "--detachedText" in cmd

    def test_other_mode_skips_document_capture_flags(self):
        CFG.IN_LOCAL = False
        crawler = BrowsertrixCrawler(mode="other")

        cmd = crawler.create_crawl_command("https://example.com", "crawl1")

        assert "--generateWACZ" not in cmd
        assert "--generatePDF" not in cmd


class TestRunSingleCrawlLocal:
    def test_success_builds_one_result_per_pvalue(self, monkeypatch, tmp_path, p_data):
        CFG.IN_LOCAL = True
        CFG.IN_DOCKER = False
        CFG.IN_CLOUD = False
        CFG.CRAWL_DIR = tmp_path
        crawl_dir = tmp_path / "crawl1"
        crawl_dir.mkdir()
        (crawl_dir / "out.wacz").write_bytes(b"fake wacz")

        def fake_run(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert len(results) == 1
        result = results[0]
        assert result["success"] is True
        assert result["url"] == p_data["url"]
        assert result["objId"] == "co1"
        assert result["pType"] == "privacy_policy"
        assert result["output_files"] == [str(crawl_dir / "out.wacz")]

    def test_multiple_pvalues_produce_multiple_results(self, monkeypatch, tmp_path):
        CFG.IN_LOCAL = True
        CFG.CRAWL_DIR = tmp_path
        p_data = {
            "url": "https://example.com/shared",
            "pValues": [
                {"objId": "co1", "name": "Co1", "pType": "privacy_policy"},
                {"objId": "co2", "name": "Co2", "pType": "privacy_policy"},
            ],
        }
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
        )
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert {r["objId"] for r in results} == {"co1", "co2"}

    def test_failure_returns_single_error_result(
        self, monkeypatch, tmp_path, p_data, caplog
    ):
        CFG.IN_LOCAL = True
        CFG.CRAWL_DIR = tmp_path
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                [], returncode=1, stdout="oops", stderr="err"
            ),
        )
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert len(results) == 1
        assert results[0]["success"] is False
        assert results[0]["error"] == 1
        assert results[0]["pValues"] == p_data["pValues"]

    def test_timeout_returns_error_result(self, monkeypatch, tmp_path, p_data):
        CFG.IN_LOCAL = True
        CFG.CRAWL_DIR = tmp_path
        CFG.TIMEOUT = 5

        def raise_timeout(cmd, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert results[0]["success"] is False
        assert "Timeout" in results[0]["error"]

    def test_generic_exception_returns_error_result(
        self, monkeypatch, tmp_path, p_data
    ):
        CFG.IN_LOCAL = True
        CFG.CRAWL_DIR = tmp_path

        def boom(*a, **k):
            raise RuntimeError("docker daemon not running")

        monkeypatch.setattr(subprocess, "run", boom)
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert results[0]["success"] is False
        assert "docker daemon not running" in results[0]["error"]

    def test_missing_crawl_dir_yields_empty_output_files(
        self, monkeypatch, tmp_path, p_data
    ):
        CFG.IN_LOCAL = True
        CFG.CRAWL_DIR = tmp_path  # crawl1 subdir never created
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
        )
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert results[0]["output_files"] == []


class TestRunSingleCrawlDocker:
    def test_success(self, monkeypatch, tmp_path, p_data):
        CFG.IN_LOCAL = False
        CFG.IN_DOCKER = True
        CFG.IN_CLOUD = False
        CFG.CRAWL_DIR = tmp_path

        class FakeContainer:
            def wait(self, timeout):
                return {"StatusCode": 0}

        class FakeContainers:
            def run(self, *a, **k):
                return FakeContainer()

        class FakeDockerClient:
            containers = FakeContainers()

        monkeypatch.setattr(docker, "from_env", lambda: FakeDockerClient())
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert results[0]["success"] is True
        assert results[0]["objId"] == "co1"

    def test_container_error_returns_clean_failure_result(
        self, monkeypatch, tmp_path, p_data
    ):
        CFG.IN_LOCAL = False
        CFG.IN_DOCKER = True
        CFG.IN_CLOUD = False
        CFG.CRAWL_DIR = tmp_path

        error = docker.errors.ContainerError(  # type: ignore
            container="fake",
            exit_status=137,
            command=["crawl"],
            image="img",
            stderr=b"died",
        )

        class FakeContainers:
            def run(self, *a, **k):
                raise error

        class FakeDockerClient:
            containers = FakeContainers()

        monkeypatch.setattr(docker, "from_env", lambda: FakeDockerClient())
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert results[0]["success"] is False
        assert results[0]["error"] == 137
        assert results[0]["pValues"] == p_data["pValues"]

    def test_generic_exception(self, monkeypatch, tmp_path, p_data):
        CFG.IN_LOCAL = False
        CFG.IN_DOCKER = True
        CFG.IN_CLOUD = False
        CFG.CRAWL_DIR = tmp_path

        class FakeContainers:
            def run(self, *a, **k):
                raise RuntimeError("no daemon")

        class FakeDockerClient:
            containers = FakeContainers()

        monkeypatch.setattr(docker, "from_env", lambda: FakeDockerClient())
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert results[0]["success"] is False
        assert "no daemon" in results[0]["error"]


class TestRunSingleCrawlCloud:
    def test_success(self, monkeypatch, tmp_path, p_data):
        CFG.IN_LOCAL = False
        CFG.IN_DOCKER = False
        CFG.IN_CLOUD = True
        CFG.CRAWL_DIR = tmp_path
        CFG.GCLOUD_PROJECT = "proj"
        CFG.GCLOUD_REGION = "us-east4"
        CFG.GCLOUD_JOB = "job"

        from google.cloud import run_v2

        captured = {}

        class FakeOperation:
            def result(self, timeout=None):
                captured["timeout"] = timeout
                return None

        class FakeJobsClient:
            def run_job(self, request):
                captured["request"] = request
                return FakeOperation()

        monkeypatch.setattr(run_v2, "JobsClient", lambda: FakeJobsClient())
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert results[0]["success"] is True
        assert captured["request"].name == "projects/proj/locations/us-east4/jobs/job"
        assert captured["timeout"] == CFG.TIMEOUT

    def test_job_execution_timeout(self, monkeypatch, tmp_path, p_data):
        # operation.result() is bounded by CFG.TIMEOUT so a stuck Cloud Run job
        # execution can't hang this call forever the way it used to.
        import concurrent.futures

        CFG.IN_LOCAL = False
        CFG.IN_DOCKER = False
        CFG.IN_CLOUD = True
        CFG.CRAWL_DIR = tmp_path
        CFG.GCLOUD_PROJECT = "proj"
        CFG.GCLOUD_REGION = "us-east4"
        CFG.GCLOUD_JOB = "job"

        from google.cloud import run_v2

        class FakeOperation:
            def result(self, timeout=None):
                raise concurrent.futures.TimeoutError()

        class FakeJobsClient:
            def run_job(self, request):
                return FakeOperation()

        monkeypatch.setattr(run_v2, "JobsClient", lambda: FakeJobsClient())
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert results[0]["success"] is False
        assert str(CFG.TIMEOUT) in results[0]["error"]

    def test_generic_exception(self, monkeypatch, tmp_path, p_data):
        CFG.IN_LOCAL = False
        CFG.IN_DOCKER = False
        CFG.IN_CLOUD = True
        CFG.CRAWL_DIR = tmp_path

        from google.cloud import run_v2

        class FakeJobsClient:
            def run_job(self, request):
                raise RuntimeError("job not found")

        monkeypatch.setattr(run_v2, "JobsClient", lambda: FakeJobsClient())
        crawler = BrowsertrixCrawler()

        results = crawler.run_single_crawl(p_data, "crawl1")

        assert results[0]["success"] is False
        assert "job not found" in results[0]["error"]


class TestRunSingleCrawlUnknownEnvironment:
    def test_exits_when_no_environment_flag_set(self, p_data):
        CFG.IN_LOCAL = False
        CFG.IN_DOCKER = False
        CFG.IN_CLOUD = False
        crawler = BrowsertrixCrawler()

        with pytest.raises(SystemExit):
            crawler.run_single_crawl(p_data, "crawl1")
