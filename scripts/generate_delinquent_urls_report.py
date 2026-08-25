# Transparency Archiver - Online Document Capture Tool
# Turns scripts/find_bad_captures.py's flagged-capture report into a
# human-readable write-up of "delinquent" company/doc-type pairs: URLs that
# have been mis-captured (homepage, login wall, error page, etc. instead of
# the actual policy) repeatedly across many historical snapshots, which
# points at a broken doc_urls entry or an uncrawlable page rather than a
# one-off fluke.
#
# Joins the flagged report back against the live companies and documents_v2
# collections to attach company names, the currently configured doc_urls
# entry, and the original_url actually captured at each bad snapshot.
#
# Usage:
#   python scripts/generate_delinquent_urls_report.py \
#       --flagged scripts/data/bad_captures_report.jsonl \
#       --output scripts/data/delinquent_urls_report.md

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

DEFAULT_DB_NAME = "transparencydb_dev"
REPEAT_OFFENDER_THRESHOLD = 2


def load_jsonl(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def fetch_context(rows: list[dict], mongodb_url: str, db_name: str) -> dict:
    """
    Pulls in whatever the flagged report doesn't already carry: each
    document's actual captured original_url and date_fetched, plus each
    company's name and currently configured doc_urls entry.
    """
    from pymongo import MongoClient
    from pymongo.server_api import ServerApi

    client = MongoClient(
        mongodb_url, server_api=ServerApi("1", strict=True, deprecation_errors=True)
    )
    try:
        documents = client[db_name]["documents_v2"]
        companies = client[db_name]["companies"]

        doc_ids = [ObjectId(row["_id"]) for row in rows]
        docs_by_id = {
            str(d["_id"]): d for d in documents.find({"_id": {"$in": doc_ids}})
        }

        company_ids = list({row["company_id"] for row in rows})
        # company_id is stored as a string on documents_v2 but as an
        # ObjectId _id on companies -- try both so this works regardless of
        # which representation is in play for a given record.
        object_ids = []
        for cid in company_ids:
            try:
                object_ids.append(ObjectId(cid))
            except Exception:
                pass
        companies_by_id = {
            str(c["_id"]): c
            for c in companies.find({"_id": {"$in": object_ids or company_ids}})
        }

        return {"docs_by_id": docs_by_id, "companies_by_id": companies_by_id}
    finally:
        client.close()


def group_rows(rows: list[dict]) -> dict:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["company_id"], row["type"])].append(row)
    return groups


def configured_url_for(company: dict, doc_type: str) -> str:
    """
    company["doc_urls"] is keyed by the original display name (e.g. "Privacy
    Policy"), while documents_v2's "type" field is that same name normalized
    the way source.utils.format_data does it: spaces to underscores,
    lowercased. Normalize doc_urls the same way before looking up doc_type.
    """
    normalized = {
        key.replace(" ", "_").lower(): url
        for key, url in company.get("doc_urls", {}).items()
    }
    return normalized.get(doc_type, "unknown")


def format_date(doc: dict | None) -> str:
    if not doc or not doc.get("date_fetched"):
        return "unknown"
    value = doc["date_fetched"]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return str(value)


def render_report(rows: list[dict], context: dict) -> str:
    docs_by_id = context["docs_by_id"]
    companies_by_id = context["companies_by_id"]
    groups = group_rows(rows)

    repeat_offenders = {
        k: v for k, v in groups.items() if len(v) >= REPEAT_OFFENDER_THRESHOLD
    }
    one_offs = {k: v for k, v in groups.items() if len(v) < REPEAT_OFFENDER_THRESHOLD}

    lines = []
    lines.append("# Delinquent URL Report")
    lines.append("")
    lines.append(
        "Generated from the policy-document classifier's sweep of the live "
        "`documents_v2` collection. These are captures the classifier is "
        "confident are NOT the policy document they claim to be (homepage, "
        "login wall, error page, cookie banner, font-license file, etc. "
        "captured instead of the real policy). Nothing has been deleted -- "
        "this is a worklist for fixing the underlying `doc_urls` entries "
        "and, separately, cleaning up the bad historical snapshots."
    )
    lines.append("")
    lines.append(
        f"**{len(rows)}** flagged captures across **{len(groups)}** distinct "
        f"company/doc-type pairs (**{len({k[0] for k in groups})}** distinct "
        f"companies). **{len(repeat_offenders)}** of those pairs were flagged "
        f"on **{REPEAT_OFFENDER_THRESHOLD}+** separate historical snapshots "
        "-- a strong signal the source URL itself is broken, not a one-off "
        "capture glitch."
    )
    lines.append("")

    lines.append("## Repeat offenders (source URL is likely broken)")
    lines.append("")
    lines.append(
        "| Company | Doc Type | Configured URL | Bad Snapshots | Date Range | Actually Captured | Example |"
    )
    lines.append("|---|---|---|---|---|---|---|")

    def sort_key(item):
        return -len(item[1])

    for (company_id, doc_type), group in sorted(repeat_offenders.items(), key=sort_key):
        company = companies_by_id.get(company_id, {})
        company_name = company.get("name", company_id)
        configured_url = configured_url_for(company, doc_type)

        dates = sorted(format_date(docs_by_id.get(row["_id"])) for row in group)
        date_range = f"{dates[0]} to {dates[-1]}" if dates else "unknown"

        captured_urls = sorted(
            {
                docs_by_id.get(row["_id"], {})
                .get("formats", {})
                .get("txt", {})
                .get("original_url", "unknown")
                for row in group
            }
        )
        captured_url_str = "; ".join(captured_urls)

        best_example = min(group, key=lambda r: r["policy_probability"])
        snippet = best_example["text_snippet"][:100].replace("|", "/")

        lines.append(
            f"| {company_name} | {doc_type} | {configured_url} | "
            f"{len(group)} | {date_range} | {captured_url_str} | {snippet}... |"
        )

    lines.append("")
    lines.append("## One-off flags (may be a single bad crawl, worth a quick look)")
    lines.append("")
    lines.append(
        "| Company | Doc Type | Configured URL | Date | Probability | Example |"
    )
    lines.append("|---|---|---|---|---|---|")

    for (company_id, doc_type), group in sorted(one_offs.items(), key=sort_key):
        company = companies_by_id.get(company_id, {})
        company_name = company.get("name", company_id)
        configured_url = configured_url_for(company, doc_type)
        row = group[0]
        date = format_date(docs_by_id.get(row["_id"]))
        snippet = row["text_snippet"][:100].replace("|", "/")
        lines.append(
            f"| {company_name} | {doc_type} | {configured_url} | {date} | "
            f"{row['policy_probability']} | {snippet}... |"
        )

    lines.append("")
    lines.append("## Recommended next steps")
    lines.append("")
    lines.append(
        "1. For repeat offenders, check whether the configured `doc_urls` entry "
        "still resolves to the real policy page in a browser -- these are the "
        "highest-value fixes since every future crawl will keep failing the "
        "same way until the URL is corrected."
    )
    lines.append(
        "2. Once a URL is fixed (or confirmed already correct and just "
        "hard to crawl), the flagged historical snapshots for that "
        "company/type are safe to delete -- see `gcs_paths_to_delete` and "
        "`_id` in `scripts/data/bad_captures_report.jsonl` for the deletion "
        "worklist."
    )
    lines.append(
        "3. One-off flags are lower priority -- likely a transient crawl "
        "issue (e.g. a temporary maintenance page) rather than a persistently "
        "broken URL, but worth a quick manual check before deleting."
    )
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate a human-readable delinquent-URLs write-up from "
        "the flagged-capture report."
    )
    parser.add_argument("--flagged", default="scripts/data/bad_captures_report.jsonl")
    parser.add_argument("--mongodb-url", default=os.getenv("MONGODB_URL"))
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--output", default="scripts/data/delinquent_urls_report.md")
    args = parser.parse_args()

    if not args.mongodb_url:
        parser.error("--mongodb-url is required (or set MONGODB_URL in .env)")

    rows = load_jsonl(Path(args.flagged))
    print(f"Loaded {len(rows)} flagged rows")

    context = fetch_context(rows, args.mongodb_url, args.db_name)
    print(
        f"Resolved {len(context['docs_by_id'])} documents and "
        f"{len(context['companies_by_id'])} companies for context"
    )

    report = render_report(rows, context)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Wrote report to {output_path}")


if __name__ == "__main__":
    main()
