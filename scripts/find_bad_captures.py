# Transparency Archiver - Online Document Capture Tool
# Read-only sweep of the live documents_v2 collection for bad (non-policy)
# captures using the classifier trained in scripts/train_policy_classifier.py
# (see source/policy_classifier.py). This script never deletes or modifies
# anything in Mongo or GCS -- it only logs candidates to a JSONL report so
# they can be reviewed and deleted later.
#
# Every document that has a .txt format is fetched from GCS and classified.
# Documents below --threshold policy-document probability are logged with
# enough detail (Mongo _id, GCS blob path per format) for a future deletion
# script to act on. Documents with no .txt format at all are skipped -- they
# can't be classified without reconstructing text from another format,
# which is a separate, not-yet-built part of the pipeline.
#
# Usage:
#   python scripts/find_bad_captures.py --threshold 0.3
#   python scripts/find_bad_captures.py --limit 100  # quick smoke test

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source.policy_classifier import policy_document_probability  # noqa: E402
from source.utils import decode_bytes  # noqa: E402

load_dotenv(dotenv_path=".env")

DEFAULT_DB_NAME = "transparencydb_dev"
DEFAULT_GCP_BUCKET = "transparency_hub_documents"
SNIPPET_CHARS = 300


def fetch_and_classify(
    doc: dict, bucket, threshold: float, model_path: str
) -> dict | None:
    """
    Downloads a document's .txt capture and classifies it. Returns a report
    row if it looks like a bad (non-policy) capture, otherwise None.
    """
    extension = doc["formats"]["txt"]["extension"]
    txt_path = f"{doc['path']}.{extension}"
    try:
        blob = bucket.blob(txt_path)
        raw_bytes = blob.download_as_bytes(timeout=60)
        text = decode_bytes(raw_bytes)
    except Exception as e:
        print(f"  Failed to fetch {txt_path}: {e}")
        return None

    probability = policy_document_probability(text, model_path=model_path)
    if probability >= threshold:
        return None

    all_format_paths = [
        f"{doc['path']}.{fmt_info['extension']}"
        for fmt_info in doc.get("formats", {}).values()
    ]

    return {
        "_id": str(doc["_id"]),
        "company_id": doc.get("company_id"),
        "type": doc.get("type"),
        "path": doc.get("path"),
        "public_url": doc.get("public_url"),
        "formats": list(doc.get("formats", {}).keys()),
        "gcs_paths_to_delete": all_format_paths,
        "policy_probability": round(probability, 4),
        "text_snippet": " ".join(text.split())[:SNIPPET_CHARS],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Read-only sweep of documents_v2 for bad (non-policy) captures. "
        "Logs candidates for later deletion; never deletes anything itself."
    )
    parser.add_argument("--mongodb-url", default=os.getenv("MONGODB_URL"))
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--gcp-bucket", default=DEFAULT_GCP_BUCKET)
    parser.add_argument("--model-path", default="models/policy_classifier.joblib")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.3,
        help="Flag documents with policy-document probability BELOW this "
        "value (default 0.3 -- conservative, favors precision over recall "
        "since this drives a deletion worklist).",
    )
    parser.add_argument("--output", default="scripts/data/bad_captures_report.jsonl")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--limit", type=int, default=None, help="Only scan the first N documents."
    )
    args = parser.parse_args()

    if not args.mongodb_url:
        parser.error("--mongodb-url is required (or set MONGODB_URL in .env)")

    from google.cloud.storage import Client
    from pymongo import MongoClient
    from pymongo.server_api import ServerApi

    mongo_client = MongoClient(
        args.mongodb_url,
        server_api=ServerApi("1", strict=True, deprecation_errors=True),
    )
    collection = mongo_client[args.db_name]["documents_v2"]

    query = {"formats.txt": {"$exists": True}}
    docs = list(collection.find(query))
    total_with_txt = len(docs)
    if args.limit is not None:
        docs = docs[: args.limit]
    print(
        f"Found {total_with_txt} documents with a .txt capture in {args.db_name}.documents_v2"
    )
    if args.limit is not None:
        print(f"Limiting this run to the first {len(docs)}")

    total_docs = collection.count_documents({})
    print(
        f"({total_docs - total_with_txt} of {total_docs} total documents have no .txt "
        "capture and are skipped by this sweep)"
    )

    storage_client = Client()
    bucket = storage_client.bucket(args.gcp_bucket)

    flagged = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                fetch_and_classify, doc, bucket, args.threshold, args.model_path
            ): doc
            for doc in docs
        }
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                flagged.append(result)
                print(
                    f"  FLAGGED ({result['policy_probability']:.2f}) "
                    f"{result['company_id']}/{result['type']} -- {result['path']}"
                )
            if i % 500 == 0:
                print(f"  ...{i}/{len(docs)} scanned")

    mongo_client.close()

    print(f"\nScanned {len(docs)} documents in {time.time() - t0:.1f}s")
    print(
        f"Flagged {len(flagged)} as likely non-policy captures (threshold={args.threshold})"
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for row in flagged:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote report to {output_path}")
    print(
        "\nNothing was deleted. Review the report, then use it as the input "
        "list for a future deletion script (Mongo _id + gcs_paths_to_delete "
        "per row)."
    )


if __name__ == "__main__":
    main()
