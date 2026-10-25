# Transparency Archiver - Online Document Capture Tool
# Inference wrapper for the policy-document classifier: given a captured
# .txt document, estimates whether it's actually a policy document (privacy
# policy, terms of service, community guidelines, etc.) as opposed to a bad
# capture (homepage, cookie wall, 404 page, login wall, ...).
#
# The model itself is trained offline -- see scripts/build_policy_dataset.py
# and scripts/train_policy_classifier.py -- and loaded here from disk.

import threading
from pathlib import Path
from typing import Optional

import joblib

from source.globals import logger

DEFAULT_MODEL_PATH = Path("models/policy_classifier.joblib")

# Caps the input handed to the sklearn pipeline's vectorizer, whose sparse-matrix
# construction goes through scipy/Cython (C++) internals -- an allocation failure
# there can surface as an uncaught C++ exception rather than Python's catchable
# MemoryError. The model was trained on ordinary policy-document lengths, so this
# is far above anything a real capture should produce and only guards against a
# pathologically large (e.g. malformed or duplicated) reconstruction.
MAX_CLASSIFIER_INPUT_CHARS = 50_000

_model = None
_model_path_loaded: Optional[Path] = None
_model_lock = threading.Lock()


def load_model(model_path: Optional[Path] = None):
    """
    Loads (and caches) the trained policy-document classifier pipeline.
    Re-loads only if a different model_path is requested than what's cached.
    """
    global _model, _model_path_loaded
    path = Path(model_path) if model_path else DEFAULT_MODEL_PATH

    with _model_lock:
        if _model is None or _model_path_loaded != path:
            if not path.exists():
                raise FileNotFoundError(
                    f"No policy-document classifier model found at {path}. "
                    "Run scripts/build_policy_dataset.py and "
                    "scripts/train_policy_classifier.py to train one."
                )
            logger.info(f"Loading policy-document classifier from {path}")
            _model = joblib.load(path)
            _model_path_loaded = path

    return _model


def policy_document_probability(text: str, model_path: Optional[Path] = None) -> float:
    """
    Returns the model's estimated probability (0-1) that `text` is a policy
    document, as opposed to a bad or irrelevant capture.
    """
    model = load_model(model_path)
    return float(model.predict_proba([text[:MAX_CLASSIFIER_INPUT_CHARS]])[0][1])


def is_policy_document(
    text: str, threshold: float = 0.5, model_path: Optional[Path] = None
) -> bool:
    """
    Classifies whether `text` is a policy document.

    Args:
      text (str): The document text to classify.
      threshold (float): Minimum policy-document probability required to
        return True. Defaults to 0.5. Callers that act destructively (e.g.
        deleting a stored snapshot) on a negative classification should
        raise this to reduce false "not a policy document" calls, at the
        cost of missing some genuinely bad captures.
      model_path (Path, optional): Overrides the default model location.

    Returns:
      bool: True if the estimated policy-document probability meets the
        threshold, False otherwise.
    """
    return policy_document_probability(text, model_path) >= threshold
