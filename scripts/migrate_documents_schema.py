# Transparency Archiver - Online Document Capture Tool
# One-off migration tool: collapses the old "documents" schema (one document
# per company_id + type + date_fetched + format) into the new schema (one
# document per company_id + type + date_fetched, with a nested "formats"
# dict). See AGENTS.md / README.md for the schema shapes.
#
# Usage:
#   python scripts/migrate_documents_schema.py --source json \
#       --input transparencydb_dev.documents08042026.json \
#       --output transparencydb_dev.documents_v2.json
#
#   python scripts/migrate_documents_schema.py --source mongo \
#       --insert-collection documents_v2
#
# The mongo mode reads from CFG.DB_NAME's "documents" collection (via
# source.utils.mongo_connect) and, if --insert-collection is given, writes
# the merged documents into a NEW collection -- it never modifies or drops
# the existing "documents" collection.

import argparse
import json
import re
import sys
from pathlib import Path

# A capture's on-disk filename is "<timestamp>.<extension>", where extension
# may itself contain dots (e.g. "warc.json"). This lets us recover the real
# file extension for a document directly from its own path, without having
# to assume it matches the "format" field (format "warc" has used both a
# ".warc.gz" and a ".wacz" extension over the project's history).
FILENAME_RE = re.compile(r"^(?P<timestamp>\d+(?:\.\d+)?)\.(?P<extension>.+)$")


def split_path(path: str) -> tuple[str, str]:
    """Splits an old-schema path into (base_path, extension)."""
    prefix, filename = path.rsplit("/", 1)
    match = FILENAME_RE.match(filename)
    if not match:
        raise ValueError(f"Could not parse timestamp/extension from path: {path!r}")
    return f"{prefix}/{match['timestamp']}", match["extension"]


def date_key(doc: dict):
    """Returns a hashable, comparable key for a document's date_fetched,
    regardless of whether it's a real datetime (Mongo mode) or mongoexport's
    extended-JSON {"$date": "..."} form (JSON mode)."""
    value = doc["date_fetched"]
    if isinstance(value, dict) and "$date" in value:
        return value["$date"]
    return value


def group_documents(docs: list[dict]) -> dict[tuple, list[dict]]:
    groups: dict[tuple, list[dict]] = {}
    for doc in docs:
        key = (doc["company_id"], doc["type"], date_key(doc))
        groups.setdefault(key, []).append(doc)
    return groups


def merge_group(group: list[dict]) -> tuple[dict, list[str]]:
    """Merges one group of old-schema documents into a single new-schema
    document. Returns (merged_doc, warnings)."""
    warnings = []

    base_paths = set()
    formats = {}
    original_urls = set()
    for doc in group:
        base_path, extension = split_path(doc["path"])
        base_paths.add(base_path)
        original_urls.add(doc["original_url"])
        formats[doc["format"]] = {
            "extension": extension,
            "original_url": doc["original_url"],
        }

    if len(base_paths) > 1:
        warnings.append(
            f"Group for {group[0]['company_id']}/{group[0]['type']} has mismatched "
            f"base paths across formats: {sorted(base_paths)}"
        )
    base_path = sorted(base_paths)[0]

    if len(original_urls) > 1:
        warnings.append(
            f"Group for {group[0]['company_id']}/{group[0]['type']} at "
            f"{date_key(group[0])} has {len(original_urls)} distinct original_url "
            f"values across formats: {sorted(original_urls)}"
        )

    # Preserve the txt-format document's _id, since source/changes.py's
    # "changes" collection references it via older_document_id/newer_document_id.
    survivor = next((d for d in group if d["format"] == "txt"), group[0])

    public_url_prefix, _ext = split_path(survivor["public_url"])

    merged = {
        "_id": survivor["_id"],
        "company_id": survivor["company_id"],
        "type": survivor["type"],
        "date_fetched": survivor["date_fetched"],
        "path": base_path,
        "public_url": public_url_prefix,
        "formats": formats,
    }
    return merged, warnings


def migrate(docs: list[dict]) -> tuple[list[dict], dict]:
    groups = group_documents(docs)
    merged_docs = []
    all_warnings = []
    for group in groups.values():
        merged, warnings = merge_group(group)
        merged_docs.append(merged)
        all_warnings.extend(warnings)

    stats = {
        "input_count": len(docs),
        "output_count": len(merged_docs),
        "group_count": len(groups),
        "warnings": all_warnings,
    }
    return merged_docs, stats


def load_json(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, docs: list[dict]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(docs, f, indent=2, default=str)


def run_json_mode(args):
    docs = load_json(Path(args.input))
    merged_docs, stats = migrate(docs)
    print_stats(stats)
    if args.output:
        save_json(Path(args.output), merged_docs)
        print(f"Wrote {len(merged_docs)} merged documents to {args.output}")
    return merged_docs, stats


def run_mongo_mode(args):
    # Imported lazily: this pulls in source.globals, which requires the
    # project's env/config to be set up, and we don't want JSON-mode runs
    # (e.g. CI, or a researcher previewing the transform) to need that.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from source.globals import CFG, init_globals
    from source.utils import mongo_connect

    # init_globals() runs its own argparse.parse_args() against sys.argv,
    # which doesn't know about this script's --source/--insert-collection
    # flags. Mask them out for the duration of the call (same fix
    # tests/conftest.py's _clean_argv fixture uses for pytest's own argv).
    original_argv = sys.argv
    sys.argv = [original_argv[0]]
    try:
        init_globals()
    finally:
        sys.argv = original_argv

    client = mongo_connect()
    try:
        collection = client[CFG.DB_NAME]["documents"]
        docs = list(collection.find())
        merged_docs, stats = migrate(docs)
        print_stats(stats)

        if args.insert_collection:
            target = client[CFG.DB_NAME][args.insert_collection]
            if merged_docs:
                target.insert_many(merged_docs)
            print(
                f"Inserted {len(merged_docs)} merged documents into "
                f"{CFG.DB_NAME}.{args.insert_collection} (existing 'documents' "
                "collection was not modified)"
            )
    finally:
        client.close()
    return merged_docs, stats


def print_stats(stats: dict):
    print(f"Input documents:  {stats['input_count']}")
    print(f"Output documents: {stats['output_count']} ({stats['group_count']} groups)")
    if stats["warnings"]:
        print(f"Warnings ({len(stats['warnings'])}):")
        for w in stats["warnings"]:
            print(f"  - {w}")
    else:
        print("No anomalies detected.")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate the 'documents' collection to the nested-formats schema."
    )
    parser.add_argument("--source", choices=["json", "mongo"], required=True)
    parser.add_argument("--input", help="Path to a JSON export (json mode)")
    parser.add_argument(
        "--output", help="Path to write the transformed JSON (json mode)"
    )
    parser.add_argument(
        "--insert-collection",
        help="Name of a NEW Mongo collection to insert merged documents into (mongo mode). "
        "The existing 'documents' collection is never modified.",
    )
    args = parser.parse_args()

    if args.source == "json":
        if not args.input:
            parser.error("--input is required for --source json")
        run_json_mode(args)
    else:
        run_mongo_mode(args)


if __name__ == "__main__":
    main()
