# Wayback Machine Historical Backfill - derive an already-covered list from
# an existing documents_v2 export
#
# transparencydb_dev.documents_v2.json (a mongoexport --jsonArray dump sitting
# in the repo root) turns out to already hold real historical coverage for a
# lot of (company, doc_type) pairs: 795 distinct pairs have at least one
# document, and many span a decade or more -- e.g. date_fetched values as far
# back as 2002, long before this pipeline's live crawler could have produced
# them. That's strong evidence of a prior backfill effort (manual or
# otherwise) that this job shouldn't blindly repeat.
#
# This script never touches Mongo or GCS -- it just reads the export file(s)
# locally, groups records by (company_id, type), and writes out every pair
# whose EARLIEST date_fetched is at or before --cutoff-year as a
# "pairs file" (company_id,doc_type per line) that
# archive_backfill/mark_precaptured.py can consume with --pairs-file to mark
# them precaptured in one shot.
#
# Why a year cutoff rather than "has any document at all": a single recent
# document just means the live pipeline captured it once -- not evidence of
# a historical backfill. A document dated well before this pipeline existed
# can only have gotten there through some kind of backfill, so that's the
# signal this script keys off.
#
# Usage:
#   python archive_backfill/extract_precaptured_from_export.py \
#       --export transparencydb_dev.documents_v2.json --cutoff-year 2023 \
#       --out archive_backfill/data/precaptured_pairs.csv
#
# Then review the output, and feed it to mark_precaptured.py:
#   python archive_backfill/mark_precaptured.py \
#       --pairs-file archive_backfill/data/precaptured_pairs.csv \
#       --note "Already had historical coverage as of transparencydb_dev.documents_v2.json (2026-08-04 export)" \
#       --confirm

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load_groups(export_path: Path) -> dict:
    """
    Returns {(company_id, doc_type): set(years)} built from every record in
    a mongoexport --jsonArray file (a top-level JSON array of documents,
    each with "company_id", "type", and a "date_fetched": {"$date": "..."}).
    """
    with open(export_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    groups: dict = defaultdict(set)
    skipped = 0
    for record in records:
        company_id = record.get("company_id")
        doc_type = record.get("type")
        date_str = (record.get("date_fetched") or {}).get("$date", "")
        if not (company_id and doc_type and date_str[:4].isdigit()):
            skipped += 1
            continue
        groups[(company_id, doc_type)].add(int(date_str[:4]))

    if skipped:
        print(
            f"Skipped {skipped} record(s) missing company_id/type/date_fetched in {export_path}"
        )
    return groups


def main():
    parser = argparse.ArgumentParser(
        description="Derive a precaptured (company_id, doc_type) pairs file from a documents_v2 export"
    )
    parser.add_argument(
        "--export",
        action="append",
        required=True,
        help="Path to a mongoexport --jsonArray file (repeatable, e.g. to combine multiple exports)",
    )
    parser.add_argument(
        "--cutoff-year",
        type=int,
        default=2023,
        help="A (company_id, doc_type) pair is considered precaptured if its EARLIEST "
        "date_fetched year is at or before this year (default: 2023)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="archive_backfill/data/precaptured_pairs.csv",
        help="Where to write the resulting pairs file",
    )
    args = parser.parse_args()

    combined: dict = defaultdict(set)
    for export_arg in args.export:
        for key, years in load_groups(Path(export_arg)).items():
            combined[key] |= years

    print(
        f"Loaded {len(combined)} distinct (company_id, doc_type) pair(s) across {len(args.export)} export file(s)"
    )

    selected = {
        key: years for key, years in combined.items() if min(years) <= args.cutoff_year
    }
    print(
        f"{len(selected)} pair(s) have data at or before {args.cutoff_year} -- treating these as already covered"
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for company_id, doc_type in sorted(selected):
            f.write(f"{company_id},{doc_type}\n")

    print(f"Wrote {len(selected)} pair(s) to {out_path}")
    print(
        "Review the file, then run mark_precaptured.py --pairs-file against it (with --confirm to actually write)."
    )


if __name__ == "__main__":
    main()
