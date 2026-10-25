# Transparency Archiver - Online Document Capture Tool
# Trains the policy-document classifier used to tell whether a reconstructed
# .txt capture is actually a policy document (privacy policy, terms of
# service, community guidelines, etc.) versus a bad capture (homepage,
# cookie wall, 404 page, login wall, ...).
#
# Reads the dataset produced by scripts/build_policy_dataset.py, fits a
# TF-IDF + Logistic Regression pipeline (class_weight="balanced" to offset
# the positive-heavy dataset -- there's far more confirmed policy text
# available than confirmed non-policy text), evaluates it on a held-out
# split, and saves the fitted pipeline for source/policy_classifier.py to
# load at inference time.
#
# Usage:
#   python scripts/train_policy_classifier.py \
#       --dataset scripts/data/policy_dataset.jsonl \
#       --model-out models/policy_classifier.joblib

import argparse
import json
from pathlib import Path

import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer


def load_dataset(path: Path) -> tuple[list[str], list[int]]:
    texts, labels = [], []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            texts.append(row["text"])
            labels.append(row["label"])
    return texts, labels


def build_pipeline() -> Pipeline:
    return Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    max_features=20000,
                    ngram_range=(1, 2),
                    min_df=2,
                    sublinear_tf=True,
                ),
            ),
            (
                "clf",
                LogisticRegression(
                    class_weight="balanced",
                    max_iter=2000,
                ),
            ),
        ]
    )


def main():
    parser = argparse.ArgumentParser(
        description="Train the policy-document classifier."
    )
    parser.add_argument("--dataset", default="scripts/data/policy_dataset.jsonl")
    parser.add_argument("--model-out", default="models/policy_classifier.joblib")
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    texts, labels = load_dataset(Path(args.dataset))
    print(
        f"Loaded {len(texts)} examples ({sum(labels)} positive, {len(labels) - sum(labels)} negative)"
    )

    X_train, X_test, y_train, y_test = train_test_split(
        texts,
        labels,
        test_size=args.test_size,
        random_state=args.seed,
        stratify=labels,
    )

    pipeline = build_pipeline()
    print("Training...")
    pipeline.fit(X_train, y_train)

    print("\nHeld-out evaluation:")
    y_pred = pipeline.predict(X_test)
    print(
        classification_report(
            y_test, y_pred, target_names=["not_policy_document", "policy_document"]
        )
    )
    print("Confusion matrix ([[TN, FP], [FN, TP]]):")
    print(confusion_matrix(y_test, y_pred))

    # Refit on the full dataset before saving, now that we've measured
    # held-out performance above -- no need to withhold data from the
    # model that will actually ship.
    print("\nRefitting on the full dataset for the saved model...")
    pipeline = build_pipeline()
    pipeline.fit(texts, labels)

    model_out = Path(args.model_out)
    model_out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, model_out)
    print(f"Saved trained pipeline to {model_out}")


if __name__ == "__main__":
    main()
