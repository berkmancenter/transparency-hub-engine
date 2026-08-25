# Tests for source/globals.py: the AppConfig dataclass and init_globals(),
# which parses env vars / CLI args / config.yaml into the shared CFG singleton.

import logging
import sys

import pytest
import yaml

from source.globals import CFG, AppConfig, init_globals


def write_config(tmp_path, overrides=None):
    config = {
        "USE_LOCAL_COMPANIES": False,
        "COMPANIES_JSON": "db/companies.json",
        "FUZZY_RATIO": 90,
        "USE_MONGO": True,
        "DB_NAME": "test_db",
        "DOCUMENTS_BSON": "db/documents.bson",
        "CHANGES_BSON": "db/changes.bson",
        "USE_GCP": True,
        "MAX_WORKERS": 2,
        "TIMEOUT": 60,
        "CREATE_CHANGES": True,
        "TOPIC_PATTERNS": {"Cookies": [r"\bcookies?\b"]},
        "PRIORITY_DICT": {"abc123": 3},
        "USE_SLACK": False,
        "USE_POLICY_CLASSIFIER": True,
        "POLICY_CLASSIFIER_THRESHOLD": 0.3,
    }
    if overrides:
        config.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def base_argv(tmp_path, config_path, extra=None):
    argv = [
        "prog",
        "--temp_dir",
        str(tmp_path / "tmp"),
        "--captures_dir",
        str(tmp_path / "captures"),
        "--changes_dir",
        str(tmp_path / "changes"),
        "--log_dir",
        str(tmp_path / "logs"),
        "--config",
        str(config_path),
    ]
    if extra:
        argv += extra
    return argv


class TestAppConfigDefaults:
    def test_dataclass_defaults(self):
        cfg = AppConfig()
        assert cfg.DRY_RUN is True
        assert cfg.USE_MONGO is True
        assert cfg.USE_GCP is True
        assert cfg.USE_SLACK is False
        assert cfg.FUZZY_THRESHOLD == 97
        assert cfg.MAX_WORKERS == 2
        assert cfg.TIMEOUT == 1200
        assert cfg.IN_LOCAL is True
        assert cfg.IN_CLOUD is False
        assert cfg.IN_DOCKER is False
        assert cfg.RECOVERY_DATA == []
        assert cfg.TOPIC_PATTERNS == {}
        assert cfg.PRIORITY_DICT == {}
        assert cfg.USE_POLICY_CLASSIFIER is True
        assert cfg.POLICY_CLASSIFIER_THRESHOLD == 0.3
        assert cfg.USE_DIFF_TRIMMER is False
        assert cfg.DIFF_TRIMMER_THRESHOLD == 0.85

    def test_mutable_defaults_are_independent_per_instance(self):
        a, b = AppConfig(), AppConfig()
        a.RECOVERY_DATA.append("http://example.com")
        assert b.RECOVERY_DATA == []


class TestEnvironmentDetection:
    def test_cloud_run_execution_sets_cloud_mode(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLOUD_RUN_EXECUTION", "1")
        monkeypatch.delenv("RUNNING_IN_DOCKER", raising=False)
        config_path = write_config(tmp_path)
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.IN_CLOUD is True
        assert CFG.IN_LOCAL is False
        assert CFG.IN_DOCKER is False

    def test_cloud_run_forced_dry_run_is_overwritten_by_argparse(
        self, monkeypatch, tmp_path
    ):
        """CLOUD_RUN_EXECUTION forces CFG.DRY_RUN = False early on, but the
        later `CFG.DRY_RUN = args.dry_run` assignment always wins -- so an
        explicit DRY_RUN=true env var still wins over the cloud-mode default.
        This locks in that (surprising) precedence rather than the docstring
        intent."""
        monkeypatch.setenv("CLOUD_RUN_EXECUTION", "1")
        monkeypatch.setenv("DRY_RUN", "true")
        config_path = write_config(tmp_path)
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.IN_CLOUD is True
        assert CFG.DRY_RUN is True

    def test_running_in_docker_sets_docker_mode(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
        monkeypatch.setenv("RUNNING_IN_DOCKER", "true")
        config_path = write_config(tmp_path)
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.IN_DOCKER is True
        assert CFG.IN_LOCAL is False
        assert CFG.IN_CLOUD is False

    def test_no_env_vars_defaults_to_local(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
        monkeypatch.delenv("RUNNING_IN_DOCKER", raising=False)
        config_path = write_config(tmp_path)
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.IN_LOCAL is True
        assert CFG.IN_CLOUD is False
        assert CFG.IN_DOCKER is False

    def test_unknown_environment_exits(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
        monkeypatch.delenv("RUNNING_IN_DOCKER", raising=False)
        # Force the "no environment could be detected" branch.
        CFG.IN_LOCAL = False
        config_path = write_config(tmp_path)
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        with pytest.raises(SystemExit) as exc_info:
            init_globals()

        assert exc_info.value.code == 1


class TestCliAndEnvArgs:
    def test_cli_overrides_create_directories(self, monkeypatch, tmp_path):
        config_path = write_config(tmp_path)
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.TEMP_DIR == tmp_path / "tmp"
        assert CFG.CRAWL_DIR == tmp_path / "tmp"
        assert CFG.DOCS_DIR == tmp_path / "tmp" / "docs"
        assert CFG.CAPTURES_DIR == tmp_path / "captures"
        assert CFG.CHANGES_DIR == tmp_path / "changes"
        assert CFG.LOG_DIR == tmp_path / "logs"
        for d in (
            CFG.TEMP_DIR,
            CFG.DOCS_DIR,
            CFG.CRAWL_DIR,
            CFG.CAPTURES_DIR,
            CFG.LOG_DIR,
        ):
            assert d.is_dir()
        assert (CFG.CAPTURES_DIR / CFG.CHANGES_DIR).is_dir()

    def test_env_var_defaults_used_when_no_cli_flag(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        config_path = write_config(tmp_path)
        monkeypatch.setenv("MONGODB_URL", "mongodb://example-host:27017/")
        monkeypatch.setenv("GCP_BUCKET", "env-bucket")
        monkeypatch.setenv("GCLOUD_PROJECT", "env-project")
        monkeypatch.setenv("GCLOUD_REGION", "us-west1")
        monkeypatch.setenv("GCLOUD_JOB", "env-job")
        monkeypatch.setattr(sys, "argv", ["prog", "--config", str(config_path)])

        init_globals()

        assert CFG.MONGODB_URL == "mongodb://example-host:27017/"
        assert CFG.GCP_BUCKET == "env-bucket"
        assert CFG.GCLOUD_PROJECT == "env-project"
        assert CFG.GCLOUD_REGION == "us-west1"
        assert CFG.GCLOUD_JOB == "env-job"

    def test_sync_priority_cli_override(self, monkeypatch, tmp_path):
        config_path = write_config(tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            base_argv(tmp_path, config_path, extra=["--sync_priority", "3"]),
        )

        init_globals()

        assert CFG.SYNC_PRIORITY == 3

    def test_dry_run_cli_flag_parses_false_correctly(self, monkeypatch, tmp_path):
        config_path = write_config(tmp_path)
        monkeypatch.setattr(
            sys, "argv", base_argv(tmp_path, config_path, extra=["--dry_run", "false"])
        )

        init_globals()

        assert CFG.DRY_RUN is False

    def test_dry_run_cli_flag_parses_true_correctly(self, monkeypatch, tmp_path):
        config_path = write_config(tmp_path)
        monkeypatch.setattr(
            sys, "argv", base_argv(tmp_path, config_path, extra=["--dry_run", "true"])
        )

        init_globals()

        assert CFG.DRY_RUN is True

    def test_dry_run_respects_env_var_when_flag_omitted(self, monkeypatch, tmp_path):
        config_path = write_config(tmp_path)
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.DRY_RUN is True


class TestConfigYamlLoading:
    def test_values_loaded_from_config(self, monkeypatch, tmp_path):
        config_path = write_config(tmp_path)
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.FUZZY_THRESHOLD == 90
        assert CFG.DB_NAME == "test_db"
        assert CFG.MAX_WORKERS == 2
        assert CFG.TIMEOUT == 60
        assert CFG.CREATE_CHANGES is True
        assert CFG.PRIORITY_DICT == {"abc123": 3}
        assert CFG.USE_SLACK is False
        assert CFG.USE_POLICY_CLASSIFIER is True
        assert CFG.POLICY_CLASSIFIER_THRESHOLD == 0.3
        assert CFG.USE_DIFF_TRIMMER is False
        assert CFG.DIFF_TRIMMER_THRESHOLD == 0.85
        assert "Cookies" in CFG.TOPIC_PATTERNS
        pattern = CFG.TOPIC_PATTERNS["Cookies"][0]
        assert pattern.search("We use Cookies.") is not None
        assert pattern.flags & __import__("re").IGNORECASE

    def test_missing_keys_fall_back_to_defaults(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("{}\n")
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.FUZZY_THRESHOLD == 97
        assert CFG.MAX_WORKERS == 4
        assert CFG.TIMEOUT == 1200
        assert CFG.CREATE_CHANGES is True
        assert CFG.USE_MONGO is True
        assert CFG.USE_GCP is True
        assert CFG.TOPIC_PATTERNS == {}
        assert CFG.PRIORITY_DICT == {}
        assert CFG.USE_SLACK is False
        assert CFG.USE_LOCAL_COMPANIES is False
        assert CFG.USE_POLICY_CLASSIFIER is True
        assert CFG.POLICY_CLASSIFIER_THRESHOLD == 0.3
        assert CFG.USE_DIFF_TRIMMER is False
        assert CFG.DIFF_TRIMMER_THRESHOLD == 0.85

    def test_policy_classifier_settings_loaded_from_config(self, monkeypatch, tmp_path):
        config_path = write_config(
            tmp_path,
            overrides={
                "USE_POLICY_CLASSIFIER": False,
                "POLICY_CLASSIFIER_THRESHOLD": 0.5,
            },
        )
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.USE_POLICY_CLASSIFIER is False
        assert CFG.POLICY_CLASSIFIER_THRESHOLD == 0.5

    def test_diff_trimmer_settings_loaded_from_config(self, monkeypatch, tmp_path):
        config_path = write_config(
            tmp_path,
            overrides={
                "USE_DIFF_TRIMMER": True,
                "DIFF_TRIMMER_THRESHOLD": 0.7,
            },
        )
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.USE_DIFF_TRIMMER is True
        assert CFG.DIFF_TRIMMER_THRESHOLD == 0.7

    def test_use_gcp_forced_false_in_cloud_mode(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLOUD_RUN_EXECUTION", "1")
        config_path = write_config(tmp_path, overrides={"USE_GCP": True})
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        init_globals()

        assert CFG.IN_CLOUD is True
        assert CFG.USE_GCP is False

    def test_missing_config_file_exits(self, monkeypatch, tmp_path, caplog):
        missing_path = tmp_path / "does_not_exist.yaml"
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, missing_path))

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc_info:
                init_globals()

        assert exc_info.value.code == 1
        assert "Loading config failed" in caplog.text

    def test_invalid_yaml_exits(self, monkeypatch, tmp_path):
        bad_config = tmp_path / "config.yaml"
        bad_config.write_text("USE_MONGO: [true\n")  # unbalanced -> YAML error
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, bad_config))

        with pytest.raises(SystemExit) as exc_info:
            init_globals()

        assert exc_info.value.code == 1


class TestRecoveryFile:
    def test_recovery_data_loaded_when_present(self, monkeypatch, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "recovery_log.txt").write_text(
            "https://a.example\nhttps://b.example\n"
        )
        config_path = write_config(tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "prog",
                "--temp_dir",
                str(tmp_path / "tmp"),
                "--captures_dir",
                str(tmp_path / "captures"),
                "--changes_dir",
                str(tmp_path / "changes"),
                "--log_dir",
                str(log_dir),
                "--config",
                str(config_path),
            ],
        )

        init_globals()

        assert CFG.RECOVERY_DATA == ["https://a.example", "https://b.example"]

    def test_recovery_data_defaults_empty_when_missing(
        self, monkeypatch, tmp_path, caplog
    ):
        config_path = write_config(tmp_path)
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))

        with caplog.at_level(logging.ERROR):
            init_globals()

        assert CFG.RECOVERY_DATA == []
        assert "No recovery file found" in caplog.text

    def test_recovery_file_unreadable_exits(self, monkeypatch, tmp_path):
        log_dir = tmp_path / "logs"
        # Make "recovery_log.txt" a directory instead of a file so open("r")
        # raises something other than FileNotFoundError.
        (log_dir / "recovery_log.txt").mkdir(parents=True)
        config_path = write_config(tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "prog",
                "--temp_dir",
                str(tmp_path / "tmp"),
                "--captures_dir",
                str(tmp_path / "captures"),
                "--changes_dir",
                str(tmp_path / "changes"),
                "--log_dir",
                str(log_dir),
                "--config",
                str(config_path),
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            init_globals()

        assert exc_info.value.code == 1


class TestWarnings:
    def test_warns_on_default_gcp_bucket_and_project(
        self, monkeypatch, tmp_path, caplog
    ):
        config_path = write_config(tmp_path, overrides={"USE_GCP": True})
        monkeypatch.setattr(sys, "argv", base_argv(tmp_path, config_path))
        # gcp_bucket / gcloud_project left at their argparse defaults.

        with caplog.at_level(logging.WARNING):
            init_globals()

        assert "default gcp bucket" in caplog.text
        assert "default gcloud project" in caplog.text
