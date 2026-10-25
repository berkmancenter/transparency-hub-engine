# Transparency Archiver - Online Document Capture Tool
# Evaluates the trained boundary classifier (models/boundary_classifier.joblib)
# against scripts/boundary_validation_ground_truth.json -- a hand-verified
# set of real capture boundaries, independent of the DOM-landmark/repetition/
# itemized-heading weak-labeling heuristics used to build the training set.
#
# Reports, per document: predicted vs. true (start, end), block-count and
# char-count drift, and whether the model UNDER-trimmed (safe -- some
# boilerplate left in) or OVER-trimmed into real content (unsafe -- would
# have cut a genuine part of the document out of the diff).
#
# Usage:
#   python scripts/evaluate_boundary_validation_set.py \
#       --ground-truth scripts/boundary_validation_ground_truth.json \
#       --model models/boundary_classifier.joblib

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate the boundary classifier against the hand-verified validation set."
    )
    parser.add_argument(
        "--ground-truth", default="scripts/boundary_validation_ground_truth.json"
    )
    parser.add_argument("--model", default="models/boundary_classifier.joblib")
    parser.add_argument("--gcp-bucket", default="transparency_hub_documents")
    parser.add_argument("--threshold", type=float, default=None)
    args = parser.parse_args()

    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from google.cloud.storage import Client
    from source import boundary_classifier as bc

    gt = json.load(open(args.ground_truth, "r", encoding="utf-8"))
    docs = gt["documents"]

    client = Client()
    bucket = client.bucket(args.gcp_bucket)

    rows = []
    for doc in docs:
        path = doc["path"]
        try:
            html = bucket.blob(path).download_as_bytes(timeout=60)
            blocks = bc.extract_blocks(html)
        except Exception as e:
            print(f"SKIP {path}: {e}")
            continue

        if len(blocks) != doc["n"]:
            print(
                f"WARNING {path}: block count drifted ({doc['n']} -> {len(blocks)}); "
                f"the capture or extractor changed since ground truth was built"
            )

        pred_start, pred_end = bc.get_boundary_trim_range(
            blocks, threshold=args.threshold, model_path=args.model
        )
        true_start, true_end = doc["start"], doc["end"]

        total_chars = sum(b.char_len for b in blocks)
        true_chars = sum(b.char_len for b in blocks[true_start:true_end])
        pred_chars = sum(b.char_len for b in blocks[pred_start:pred_end])

        over_trim_leading = pred_start > true_start
        over_trim_trailing = pred_end < true_end
        under_trim_leading = pred_start < true_start
        under_trim_trailing = pred_end > true_end

        rows.append(
            {
                "path": path,
                "n": len(blocks),
                "true": (true_start, true_end),
                "pred": (pred_start, pred_end),
                "exact_match": (pred_start, pred_end) == (true_start, true_end),
                "over_trim_leading": over_trim_leading,
                "over_trim_trailing": over_trim_trailing,
                "under_trim_leading": under_trim_leading,
                "under_trim_trailing": under_trim_trailing,
                "true_pct_kept": true_chars / total_chars if total_chars else 0,
                "pred_pct_kept": pred_chars / total_chars if total_chars else 0,
            }
        )

    print(f"\nEvaluated {len(rows)}/{len(docs)} documents\n")
    print(f"{'path':60s} {'true kept%':>10s} {'pred kept%':>10s}  flags")
    exact = 0
    unsafe = 0
    for r in rows:
        flags = []
        if r["exact_match"]:
            flags.append("EXACT")
            exact += 1
        if r["over_trim_leading"]:
            flags.append("OVER-TRIM-LEADING(unsafe)")
            unsafe += 1
        if r["over_trim_trailing"]:
            flags.append("OVER-TRIM-TRAILING(unsafe)")
            unsafe += 1
        if r["under_trim_leading"]:
            flags.append("under-trim-leading(safe)")
        if r["under_trim_trailing"]:
            flags.append("under-trim-trailing(safe)")
        print(
            f"{r['path']:60s} {r['true_pct_kept']:>9.1%} {r['pred_pct_kept']:>9.1%}  "
            f"{' '.join(flags)}"
        )

    print()
    print(f"Exact boundary match: {exact}/{len(rows)}")
    print(f"Documents with an UNSAFE over-trim (cut into real content): {unsafe}/{len(rows)}")

    output_path = Path("scripts/data/boundary_validation_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    print(f"\nWrote detailed results to {output_path}")


if __name__ == "__main__":
    main()
