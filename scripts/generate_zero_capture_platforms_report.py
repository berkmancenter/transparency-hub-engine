# Transparency Archiver - Online Document Capture Tool
# Read-only sweep of the live `companies` and `documents_v2` collections to
# find "dark" platforms/doc-types: company/doc-type pairs configured in
# `doc_urls` that currently have ZERO documents_v2 snapshots.
#
# This is distinct from scripts/generate_delinquent_urls_report.py (which
# reports captures that exist but look wrong). This script reports the
# opposite: captures that don't exist at all right now, whether because they
# were never successfully crawled, or because every historical snapshot for
# that pair was deleted by scripts/delete_flagged_captures.py (classifier
# cleanup) and/or scripts/reconstruct_missing_txt.py (txt-reconstruction
# cleanup) on 2026-08-04.
#
# To explain *why* a pair is currently empty, it cross-references the local
# audit trails those two cleanup runs left behind (scripts/data/*.jsonl,
# gitignored, so only present if those scripts have been run locally):
#   - bad_captures_report.jsonl   (classifier-flagged, then deleted)
#   - reconstruction_report.jsonl (reconstruction decisions, incl. deletes)
# Pairs with no history in either file were presumably never captured at all.
#
# Nothing is deleted or modified. Read-only.
#
# Usage:
#   python scripts/generate_zero_capture_platforms_report.py

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

DEFAULT_DB_NAME = "transparencydb_dev"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def normalize_type(doc_type: str) -> str:
    return doc_type.replace(" ", "_").lower()


def fetch_live_data(mongodb_url: str, db_name: str) -> dict:
    from pymongo import MongoClient
    from pymongo.server_api import ServerApi

    client = MongoClient(
        mongodb_url, server_api=ServerApi("1", strict=True, deprecation_errors=True)
    )
    try:
        companies = list(client[db_name]["companies"].find())
        documents = client[db_name]["documents_v2"].find(
            {}, {"company_id": 1, "type": 1, "date_fetched": 1}
        )
        current_counts = defaultdict(int)
        current_latest = {}
        for doc in documents:
            key = (str(doc["company_id"]), doc["type"])
            current_counts[key] += 1
            fetched = doc.get("date_fetched")
            if fetched is not None:
                prev = current_latest.get(key)
                if prev is None or fetched > prev:
                    current_latest[key] = fetched
        return {
            "companies": companies,
            "current_counts": current_counts,
            "current_latest": current_latest,
        }
    finally:
        client.close()


def build_deletion_history(data_dir: Path) -> dict:
    """
    Returns {(company_id, type): [reason, ...]} for every historical deletion
    found in the local cleanup audit trails, one entry per deleted snapshot.
    """
    history = defaultdict(list)

    for row in load_jsonl(data_dir / "bad_captures_report.jsonl"):
        key = (row["company_id"], row["type"])
        history[key].append(f"flagged non-policy capture (p={row['policy_probability']})")

    for row in load_jsonl(data_dir / "reconstruction_report.jsonl"):
        decision = row.get("decision", "")
        if decision.startswith("delete_"):
            key = (row["company_id"], row["type"])
            history[key].append(f"reconstruction cleanup: {decision}")

    return history


def main():
    parser = argparse.ArgumentParser(
        description="Report company/doc-type pairs configured in doc_urls "
        "that currently have zero documents_v2 snapshots."
    )
    parser.add_argument("--mongodb-url", default=os.getenv("MONGODB_URL"))
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--data-dir", default="scripts/data")
    parser.add_argument(
        "--output", default="scripts/data/zero_capture_platforms_report.md"
    )
    args = parser.parse_args()

    if not args.mongodb_url:
        parser.error("--mongodb-url is required (or set MONGODB_URL in .env)")

    print("Fetching companies and documents_v2 from Mongo...")
    live = fetch_live_data(args.mongodb_url, args.db_name)
    companies = live["companies"]
    current_counts = live["current_counts"]
    print(f"Loaded {len(companies)} companies")

    deletion_history = build_deletion_history(Path(args.data_dir))

    # gaps: per-company list of (doc_type, reason) for every configured
    # doc_urls entry with zero current documents_v2 rows.
    gaps_by_company = {}
    total_configured_pairs = 0

    for company in companies:
        company_id = str(company["_id"])
        doc_urls = company.get("doc_urls", {}) or {}
        company_gaps = []
        for doc_type_label, doc_entry in doc_urls.items():
            total_configured_pairs += 1
            norm_type = normalize_type(doc_type_label)
            key = (company_id, norm_type)
            if current_counts.get(key, 0) == 0:
                reasons = deletion_history.get(key, [])
                if reasons:
                    detail = (
                        f"{len(reasons)} historical snapshot(s) deleted -- "
                        f"{'; '.join(sorted(set(reasons)))}"
                    )
                else:
                    detail = "no history found -- likely never successfully captured"
                company_gaps.append(
                    {
                        "doc_type": doc_type_label,
                        "url": "; ".join(doc_entry.get("urls", [])),
                        "detail": detail,
                        "had_history": bool(reasons),
                    }
                )
        if company_gaps:
            gaps_by_company[company_id] = {
                "company": company,
                "gaps": company_gaps,
                "total_doc_types": len(doc_urls),
            }

    fully_dark = {
        cid: v
        for cid, v in gaps_by_company.items()
        if len(v["gaps"]) == v["total_doc_types"]
    }
    partial_gaps = {
        cid: v for cid, v in gaps_by_company.items() if cid not in fully_dark
    }

    print(f"\n{total_configured_pairs} configured company/doc-type pairs checked")
    print(f"{len(fully_dark)} platforms have ZERO captures across every configured doc type")
    print(f"{len(partial_gaps)} platforms have a gap in at least one doc type (others OK)")

    lines = []
    lines.append("# Zero-Capture Platforms Report")
    lines.append("")
    lines.append(
        "Company/doc-type pairs configured in `companies.doc_urls` that "
        "currently have **zero** rows in `documents_v2`. Generated by "
        "`scripts/generate_zero_capture_platforms_report.py` -- read-only, "
        "nothing was changed. This is a decision worklist, not an action list."
    )
    lines.append("")
    lines.append(
        f"- **{total_configured_pairs}** configured company/doc-type pairs checked\n"
        f"- **{len(fully_dark)}** platforms are fully dark (every configured doc type is empty)\n"
        f"- **{len(partial_gaps)}** platforms have a partial gap (at least one doc type is empty, "
        "others still have captures)"
    )
    lines.append("")

    lines.append("## Fully dark platforms")
    lines.append("")
    lines.append(
        "No successful capture currently exists for ANY configured document "
        "type. Candidates for: fixing a broken `doc_urls` entry, re-running "
        "the crawler manually, or removing the platform from tracking if "
        "it's no longer relevant."
    )
    lines.append("")
    if not fully_dark:
        lines.append("_None found._")
    else:
        lines.append("| Company | Doc Types (all empty) | Detail |")
        lines.append("|---|---|---|")
        for cid, v in sorted(fully_dark.items(), key=lambda kv: kv[1]["company"].get("name", "")):
            company = v["company"]
            name = company.get("name", cid)
            for gap in v["gaps"]:
                lines.append(
                    f"| {name} | {gap['doc_type']} ([link]({gap['url']})) | {gap['detail']} |"
                )
    lines.append("")

    lines.append("## Partial gaps (specific doc types missing)")
    lines.append("")
    lines.append(
        "The platform still has at least one successful capture, but the "
        "listed doc type(s) currently have none."
    )
    lines.append("")
    if not partial_gaps:
        lines.append("_None found._")
    else:
        lines.append("| Company | Missing Doc Type | Configured URL | Detail |")
        lines.append("|---|---|---|---|")
        for cid, v in sorted(partial_gaps.items(), key=lambda kv: kv[1]["company"].get("name", "")):
            company = v["company"]
            name = company.get("name", cid)
            for gap in v["gaps"]:
                lines.append(
                    f"| {name} | {gap['doc_type']} | {gap['url']} | {gap['detail']} |"
                )
    lines.append("")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nWrote report to {output_path}")


if __name__ == "__main__":
    main()
