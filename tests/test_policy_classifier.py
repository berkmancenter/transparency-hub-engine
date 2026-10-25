# Tests for source/policy_classifier.py: the inference wrapper around the
# trained policy-document classifier. These tests fake out joblib.load so
# they never touch the real model.joblib artifact on disk.

import pytest

from source import policy_classifier as pc


class FakeModel:
    """Stands in for the fitted sklearn Pipeline. predict_proba returns a
    fixed [P(not_policy), P(policy)] pair for every input."""

    def __init__(self, policy_probability):
        self.policy_probability = policy_probability

    def predict_proba(self, texts):
        return [[1 - self.policy_probability, self.policy_probability]]


@pytest.fixture(autouse=True)
def _reset_model_cache():
    """The module caches the loaded model in globals; reset it so tests
    don't leak state into each other."""
    pc._model = None
    pc._model_path_loaded = None
    yield
    pc._model = None
    pc._model_path_loaded = None


class TestLoadModel:
    def test_raises_when_model_file_missing(self, tmp_path):
        missing_path = tmp_path / "does_not_exist.joblib"
        with pytest.raises(FileNotFoundError):
            pc.load_model(missing_path)

    def test_loads_and_caches_model(self, tmp_path, monkeypatch):
        model_path = tmp_path / "model.joblib"
        model_path.touch()
        fake_model = FakeModel(0.9)
        load_calls = []

        def fake_joblib_load(path):
            load_calls.append(path)
            return fake_model

        monkeypatch.setattr(pc.joblib, "load", fake_joblib_load)

        result1 = pc.load_model(model_path)
        result2 = pc.load_model(model_path)

        assert result1 is fake_model
        assert result2 is fake_model
        # Second call should hit the cache, not reload from disk
        assert len(load_calls) == 1

    def test_reloads_when_path_changes(self, tmp_path, monkeypatch):
        path_a = tmp_path / "a.joblib"
        path_b = tmp_path / "b.joblib"
        path_a.touch()
        path_b.touch()
        monkeypatch.setattr(pc.joblib, "load", lambda path: FakeModel(0.5))

        pc.load_model(path_a)
        assert pc._model_path_loaded == path_a

        pc.load_model(path_b)
        assert pc._model_path_loaded == path_b


class TestPolicyDocumentProbability:
    def test_returns_model_probability(self, tmp_path, monkeypatch):
        model_path = tmp_path / "model.joblib"
        model_path.touch()
        monkeypatch.setattr(pc.joblib, "load", lambda path: FakeModel(0.73))

        prob = pc.policy_document_probability("some text", model_path)

        assert prob == pytest.approx(0.73)


class TestIsPolicyDocument:
    def test_true_when_probability_meets_default_threshold(self, tmp_path, monkeypatch):
        model_path = tmp_path / "model.joblib"
        model_path.touch()
        monkeypatch.setattr(pc.joblib, "load", lambda path: FakeModel(0.6))

        assert pc.is_policy_document("some text", model_path=model_path) is True

    def test_false_when_probability_below_default_threshold(
        self, tmp_path, monkeypatch
    ):
        model_path = tmp_path / "model.joblib"
        model_path.touch()
        monkeypatch.setattr(pc.joblib, "load", lambda path: FakeModel(0.4))

        assert pc.is_policy_document("some text", model_path=model_path) is False

    def test_higher_threshold_rejects_borderline_probability(
        self, tmp_path, monkeypatch
    ):
        model_path = tmp_path / "model.joblib"
        model_path.touch()
        monkeypatch.setattr(pc.joblib, "load", lambda path: FakeModel(0.8))

        assert (
            pc.is_policy_document("some text", threshold=0.9, model_path=model_path)
            is False
        )
