# Transparency Archiver - Online Document Capture Tool
# Folds confirmed-bad captures found by scripts/find_bad_captures.py back
# into the training dataset as negative (non-policy) examples, correcting
# their original label=1 (they were treated as ground-truth positives in
# scripts/build_policy_dataset.py, since they came from documents_v2's own
# .txt captures -- but a human has now reviewed find_bad_captures.py's
# report and confirmed these specific captures are actually bad, e.g. a
# homepage or marketing page captured instead of the real policy).
#
# This is the review step of an active-learning loop: the classifier finds
# hard/ambiguous cases, a human confirms them, and the confirmed labels feed
# back into the next training run.
#
# Usage:
#   python scripts/apply_flagged_negatives.py \
#       --dataset scripts/data/policy_dataset.jsonl \
#       --flagged scripts/data/bad_captures_report.jsonl \
#       --output scripts/data/policy_dataset.jsonl

import argparse
import json
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def gcs_source_base_path(source: str) -> str | None:
    """Strips the trailing '.<extension>' off a 'gcs:<path>.<ext>' source
    tag to recover the bare document path used in the flagged report."""
    if not source.startswith("gcs:"):
        return None
    gcs_path = source[len("gcs:") :]
    return gcs_path.rsplit(".", 1)[0]


def main():
    parser = argparse.ArgumentParser(
        description="Relabel confirmed-bad captures as negative examples "
        "in the training dataset."
    )
    parser.add_argument("--dataset", default="scripts/data/policy_dataset.jsonl")
    parser.add_argument("--flagged", default="scripts/data/bad_captures_report.jsonl")
    parser.add_argument("--output", default="scripts/data/policy_dataset.jsonl")
    args = parser.parse_args()

    dataset = load_jsonl(Path(args.dataset))
    flagged = load_jsonl(Path(args.flagged))
    flagged_paths = {row["path"] for row in flagged}

    relabeled = 0
    for row in dataset:
        base_path = gcs_source_base_path(row.get("source", ""))
        if base_path in flagged_paths and row["label"] != 0:
            row["label"] = 0
            row["relabeled_from_flagged_report"] = True
            relabeled += 1

    matched_paths = {
        gcs_source_base_path(row.get("source", ""))
        for row in dataset
        if row.get("relabeled_from_flagged_report")
    }
    unmatched = flagged_paths - matched_paths
    if unmatched:
        print(f"Warning: {len(unmatched)} flagged paths had no dataset match:")
        for path in sorted(unmatched):
            print(f"  {path}")

    print(f"Relabeled {relabeled} examples from positive to negative")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for row in dataset:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote updated dataset to {output_path}")


if __name__ == "__main__":
    main()
