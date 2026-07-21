# Tests for main.py: the top-level orchestration of a single capture run.
# Every collaborator is faked out here -- this only verifies main() calls
# the right things, in the right order, gated by the right CFG flags.

from typing import Optional

import pytest

import main as main_module
from source.globals import CFG


class FakePipeline:
    last_instance: Optional["FakePipeline"] = None

    def __init__(self, company_data):
        self.company_data = company_data
        FakePipeline.last_instance = self

    def process_all_documents(self):
        return {"co1", "co2"}


class TestMain:
    def test_full_orchestration_runs_in_order(self, monkeypatch):
        CFG.DRY_RUN = False
        CFG.CREATE_CHANGES = True
        calls = []

        monkeypatch.setattr(
            main_module,
            "get_validated_companies",
            lambda: calls.append("get_validated_companies") or ["company"],
        )
        monkeypatch.setattr(
            main_module,
            "format_data",
            lambda data: calls.append("format_data") or [("url", ["p"])],
        )
        monkeypatch.setattr(main_module, "DocumentPipeline", FakePipeline)
        monkeypatch.setattr(
            main_module,
            "create_changes_json",
            lambda id_set: calls.append(("create_changes_json", id_set)),
        )
        monkeypatch.setattr(main_module, "cleanup", lambda: calls.append("cleanup"))

        with pytest.raises(SystemExit) as exc_info:
            main_module.main()

        assert exc_info.value.code == 0
        assert calls[0] == "get_validated_companies"
        assert calls[1] == "format_data"
        assert calls[2] == ("create_changes_json", {"co1", "co2"})
        assert calls[3] == "cleanup"
        assert FakePipeline.last_instance is not None
        assert FakePipeline.last_instance.company_data == [("url", ["p"])]

    def test_dry_run_skips_changes_json_and_cleanup(self, monkeypatch):
        CFG.DRY_RUN = True
        CFG.CREATE_CHANGES = True
        calls = []

        monkeypatch.setattr(main_module, "get_validated_companies", lambda: ["company"])
        monkeypatch.setattr(main_module, "format_data", lambda data: [])
        monkeypatch.setattr(main_module, "DocumentPipeline", FakePipeline)
        monkeypatch.setattr(
            main_module,
            "create_changes_json",
            lambda id_set: calls.append("create_changes_json"),
        )
        monkeypatch.setattr(main_module, "cleanup", lambda: calls.append("cleanup"))

        with pytest.raises(SystemExit):
            main_module.main()

        assert calls == []

    def test_create_changes_false_skips_changes_json_but_still_cleans_up(
        self, monkeypatch
    ):
        CFG.DRY_RUN = False
        CFG.CREATE_CHANGES = False
        calls = []

        monkeypatch.setattr(main_module, "get_validated_companies", lambda: ["company"])
        monkeypatch.setattr(main_module, "format_data", lambda data: [])
        monkeypatch.setattr(main_module, "DocumentPipeline", FakePipeline)
        monkeypatch.setattr(
            main_module,
            "create_changes_json",
            lambda id_set: calls.append("create_changes_json"),
        )
        monkeypatch.setattr(main_module, "cleanup", lambda: calls.append("cleanup"))

        with pytest.raises(SystemExit):
            main_module.main()

        assert calls == ["cleanup"]
