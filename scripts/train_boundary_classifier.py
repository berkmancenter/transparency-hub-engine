# Transparency Archiver - Online Document Capture Tool
# Trains the boundary classifier used to trim header/footer/nav boilerplate
# off HTML captures before that text is used for change-detection/diffing
# (see source/boundary_classifier.py, source/async_compare.py:compare_html_files).
#
# Reads the block-level dataset produced by scripts/build_boundary_dataset.py,
# fits a (TF-IDF text + structural features) + Logistic Regression pipeline,
# evaluates it on a held-out split -- with particular attention to precision
# at the high-confidence threshold the conservative trim rule actually uses,
# since that's the number that determines how safe it is to trim on --  and
# saves the fitted pipeline for source/boundary_classifier.py to load at
# inference time.
#
# Usage:
#   python scripts/train_boundary_classifier.py \
#       --dataset scripts/data/boundary_dataset.jsonl \
#       --model-out models/boundary_classifier.joblib

import argparse
import json
import sys
from pathlib import Path

import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source.boundary_classifier import (  # noqa: E402
    DEFAULT_BOILERPLATE_THRESHOLD,
    BlockFeatureExtractor,
)


def load_dataset(path: Path) -> tuple[list[dict], list[int]]:
    rows, labels = [], []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            labels.append(row.pop("label"))
            row.pop("source", None)
            row.pop("label_reason", None)
            rows.append(row)
    return rows, labels


def build_pipeline() -> Pipeline:
    return Pipeline(
        [
            (
                "features",
                FeatureUnion(
                    [
                        (
                            "text",
                            Pipeline(
                                [
                                    ("extract", BlockFeatureExtractor(mode="text")),
                                    (
                                        "tfidf",
                                        TfidfVectorizer(
                                            max_features=5000,
                                            ngram_range=(1, 2),
                                            min_df=2,
                                            sublinear_tf=True,
                                        ),
                                    ),
                                ]
                            ),
                        ),
                        ("numeric", BlockFeatureExtractor(mode="numeric")),
                    ]
                ),
            ),
            (
                "clf",
                LogisticRegression(
                    class_weight="balanced",
                    max_iter=5000,
                ),
            ),
        ]
    )


def main():
    parser = argparse.ArgumentParser(description="Train the boundary classifier.")
    parser.add_argument("--dataset", default="scripts/data/boundary_dataset.jsonl")
    parser.add_argument("--model-out", default="models/boundary_classifier.joblib")
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows, labels = load_dataset(Path(args.dataset))
    print(
        f"Loaded {len(rows)} labeled blocks "
        f"({sum(labels)} boilerplate, {len(labels) - sum(labels)} body)"
    )

    X_train, X_test, y_train, y_test = train_test_split(
        rows,
        labels,
        test_size=args.test_size,
        random_state=args.seed,
        stratify=labels,
    )

    pipeline = build_pipeline()
    print("Training...")
    pipeline.fit(X_train, y_train)

    print("\nHeld-out evaluation (threshold 0.5):")
    y_pred = pipeline.predict(X_test)
    print(classification_report(y_test, y_pred, target_names=["body", "boilerplate"]))
    print("Confusion matrix ([[TN, FP], [FN, TP]]):")
    print(confusion_matrix(y_test, y_pred))

    # The conservative trim rule (source/boundary_classifier.py trim_boilerplate_blocks)
    # only ever acts on blocks scoring at or above DEFAULT_BOILERPLATE_THRESHOLD, so that's
    # the number that actually determines how safe trimming is in production -- a high
    # score here means "when this model is confident enough to trim something, it's almost
    # always actually boilerplate," independent of its overall accuracy at threshold 0.5.
    probs = pipeline.predict_proba(X_test)[:, 1]
    confident_labels = [y for y, p in zip(y_test, probs) if p >= DEFAULT_BOILERPLATE_THRESHOLD]
    n_confident = len(confident_labels)
    if n_confident:
        confident_precision = sum(confident_labels) / n_confident
        print(
            f"\nAt trim threshold {DEFAULT_BOILERPLATE_THRESHOLD}: {n_confident}/{len(y_test)} "
            f"held-out blocks scored high enough to trim; "
            f"{confident_precision:.1%} of those were actually boilerplate."
        )
    else:
        print(
            f"\nNo held-out blocks scored at or above the trim threshold "
            f"{DEFAULT_BOILERPLATE_THRESHOLD} -- can't estimate trim precision from this split."
        )

    # Refit on the full dataset before saving, now that we've measured
    # held-out performance above -- no need to withhold data from the
    # model that will actually ship.
    print("\nRefitting on the full dataset for the saved model...")
    pipeline = build_pipeline()
    pipeline.fit(rows, labels)

    model_out = Path(args.model_out)
    model_out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, model_out)
    print(f"Saved trained pipeline to {model_out}")


if __name__ == "__main__":
    main()
