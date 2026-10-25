# Transparency Archiver - Online Document Capture Tool
# Builds a labeled, block-level dataset for training the boundary classifier
# (see scripts/train_boundary_classifier.py), which trims header/footer/nav
# boilerplate off HTML captures before they're used for change-detection.
#
# Ground truth doesn't exist anywhere in the database -- nobody hand-labels
# which paragraphs of a captured page are "the policy" vs "site chrome". This
# script synthesizes weak labels per text block (source/boundary_classifier.py
# extract_blocks) from several independent signals, tried in priority order:
#
#   1. DOM landmark (nearest of nav/header/footer/aside vs main/article) --
#      the strongest, cheapest signal when present.
#   2. Itemized-clause detection: a run of blocks immediately following a
#      heading like "Information We Collect" is body text even if it's
#      short, unlinked, and sitting late in the document -- exactly the
#      shape that was previously getting misread as a footer link list (see
#      the Samsung case in the boundary-trimmer dry run write-up).
#   3. Cross-document-type repetition: text that recurs verbatim across a
#      company's *other* document types (privacy_policy vs terms_of_service
#      vs community_guidelines, ...) is almost certainly shared site chrome,
#      regardless of markup -- and text that appears in none of a company's
#      other document types is almost certainly specific to this one. This
#      is markup-agnostic, so it also covers div-soup pages the DOM-landmark
#      signal can't see. Deliberately scoped to *other types*, not other
#      timestamps of the same type -- an unrevised policy clause recurs
#      verbatim across time too, and that must not be mistaken for chrome.
#   4. Link density / length fallback for whatever's left.
#
# Anything none of these are confident about is dropped rather than guessed
# at, since a wrong weak label would actively mislead a classifier that's
# specifically meant to be conservative.
#
# A handful of hand-written boilerplate/body snippets are added for lexical
# diversity, mirroring the SYNTHETIC_NEGATIVES pattern in build_policy_dataset.py.
#
# Usage:
#   python scripts/build_boundary_dataset.py \
#       --input transparencydb_dev.documents_v2.json \
#       --output scripts/data/boundary_dataset.jsonl

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source.boundary_classifier import (  # noqa: E402
    BODY_ANCESTOR_TAGS,
    BOILERPLATE_ANCESTOR_TAGS,
    block_to_feature_dict,
    extract_blocks,
)

# Secondary heuristic thresholds for blocks with no semantic ancestor tag and
# no itemized-clause / repetition signal either.
LINK_HEAVY_DENSITY = 0.6
LINK_HEAVY_MAX_CHARS = 300
LONG_PROSE_MIN_CHARS = 200
LONG_PROSE_MAX_DENSITY = 0.1

# A block whose text is unique to one document (never seen in any of the
# same company's other document *types*) needs at least this many characters
# to be weak-labeled as body from uniqueness alone -- long enough to be a
# real clause/sentence rather than a stray short fragment that's merely
# uncommon by chance (a one-off date stamp, a typo'd label, ...).
MIN_UNIQUE_BODY_CHARS = 15

# A block's text recurring across at least this many of a company's *other*
# document types is treated as shared site chrome.
MIN_CROSS_TYPE_REPEATS = 1

# Heading tags used to find both itemized-section starts and their ends.
HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# Matches common headings that introduce a bulleted/itemized list of data
# categories -- exactly the shape that reads structurally like a footer link
# list (short blocks, no anchors, deep in the document) but is genuine
# policy body text.
ITEMIZED_HEADING_RE = re.compile(
    r"(information (that )?(we|you) (collect|provide|gather)"
    r"|categories of (personal )?(information|data)"
    r"|types? of (personal )?(information|data)"
    r"|personal (data|information) (we|that) (collect|gather|obtain)"
    r"|what (personal )?(information|data) (do )?(we|you) collect"
    r"|data we collect)",
    re.IGNORECASE,
)

# How many blocks after a matching heading are still considered part of that
# itemized section if no further heading is found first -- a bound so one
# false heading match can't swallow the rest of the document.
ITEMIZED_SECTION_MAX_BLOCKS = 60

# Caps how many weakly-labeled blocks a single document can contribute, so one
# unusually long or heavily-structured page doesn't dominate the dataset.
MAX_BLOCKS_PER_DOC = 200

SYNTHETIC_BOILERPLATE = [
    "Home About Us Products Pricing Blog Careers Contact",
    "Terms of Service Privacy Policy Cookie Policy Sitemap",
    "© 2026 Example Corp. All rights reserved.",
    "Follow us on Facebook Twitter Instagram LinkedIn YouTube",
    "Skip to main content",
    "We use cookies to improve your experience. Accept All Manage Preferences",
    "Sign up for our newsletter to get the latest updates and offers.",
    "Download on the App Store Get it on Google Play",
    "Back to top",
    "Share this page Email Facebook Twitter LinkedIn",
    "Careers Press Investors Legal Accessibility",
    "Language: English (US) Change region",
]

SYNTHETIC_BODY = [
    (
        "We collect information you provide directly to us, such as when you "
        "create an account, update your profile, or communicate with us. This "
        "may include your name, email address, and any other information you "
        "choose to provide."
    ),
    (
        "You may terminate your account at any time by contacting customer "
        "support. We reserve the right to suspend or terminate your access to "
        "the service if you violate these terms or engage in conduct that we "
        "determine, in our sole discretion, to be harmful to other users."
    ),
    (
        "This Privacy Policy describes how we collect, use, and share "
        "information about you when you use our services. By using our "
        "services, you agree to the collection and use of information in "
        "accordance with this policy."
    ),
    (
        "Any disputes arising out of or relating to these terms will be "
        "resolved through binding arbitration rather than in court, except "
        "that you may assert claims in small claims court if your claims "
        "qualify."
    ),
]

# Hand-written itemized data-category clauses -- the exact shape that caused
# the Samsung over-trim: short, unlinked, terminally-punctuated list items
# that structurally resemble a footer link list but are genuine policy body.
SYNTHETIC_ITEMIZED_BODY = [
    "gender;",
    "date of birth;",
    "photos, contacts, and text logs stored on your device;",
    "precise geolocation of your device if you consent to location sharing;",
    "voice recordings when you use voice commands to control the service;",
    "device identifiers and advertising identifiers;",
    "username and password for linked third-party accounts;",
    "your billing address and payment card type;",
]


def is_heading(block) -> bool:
    return bool(block.ancestor_tags & HEADING_TAGS)


def mark_itemized_sections(blocks: list) -> list[bool]:
    """
    Returns a same-length list of bools: True for each block that falls
    inside a run started by a heading matching ITEMIZED_HEADING_RE and ended
    by the next heading (of any level) or ITEMIZED_SECTION_MAX_BLOCKS,
    whichever comes first. Heading blocks themselves are never marked.
    """
    flags = [False] * len(blocks)
    in_section = False
    section_start = 0
    for i, block in enumerate(blocks):
        if is_heading(block):
            in_section = bool(ITEMIZED_HEADING_RE.search(block.text))
            section_start = i + 1
            continue
        if in_section:
            if i - section_start >= ITEMIZED_SECTION_MAX_BLOCKS:
                in_section = False
                continue
            flags[i] = True
    return flags


def weak_label(block, is_itemized: bool, cross_type_repeats: int) -> tuple:
    """Returns (label, reason) or (None, None) if no signal is confident enough."""
    if block.nearest_landmark in BOILERPLATE_ANCESTOR_TAGS:
        return 1, "landmark_boilerplate"
    if block.nearest_landmark in BODY_ANCESTOR_TAGS:
        return 0, "landmark_body"
    if is_itemized:
        return 0, "itemized_heading"
    if cross_type_repeats >= MIN_CROSS_TYPE_REPEATS:
        return 1, "cross_type_repeat"
    if cross_type_repeats == 0 and block.char_len >= MIN_UNIQUE_BODY_CHARS:
        return 0, "cross_type_unique"
    if block.link_density >= LINK_HEAVY_DENSITY and block.char_len <= LINK_HEAVY_MAX_CHARS:
        return 1, "link_heavy_fallback"
    if block.char_len >= LONG_PROSE_MIN_CHARS and block.link_density <= LONG_PROSE_MAX_DENSITY:
        return 0, "long_prose_fallback"
    return None, None


def load_html_docs(input_path: Path, limit: int | None) -> list[dict]:
    with open(input_path, "r", encoding="utf-8") as f:
        docs = json.load(f)
    with_html = [d for d in docs if "html" in d.get("formats", {})]
    if limit is not None:
        with_html = with_html[:limit]
    return with_html


def fetch_and_extract(doc: dict, bucket) -> tuple:
    """Returns (path, blocks) on success, or (path, None) on failure."""
    extension = doc["formats"]["html"]["extension"]
    gcs_path = f"{doc['path']}.{extension}"
    try:
        blob = bucket.blob(gcs_path)
        html_bytes = blob.download_as_bytes(timeout=60)
        blocks = extract_blocks(html_bytes)
        return gcs_path, blocks
    except Exception as e:
        print(f"  Failed to fetch/parse {gcs_path}: {e}")
        return gcs_path, None


def label_company(doc_type_by_path: dict, blocks_by_path: dict) -> list[dict]:
    """
    Labels every block across one company's captures at once, so the
    cross-document-type repetition signal can see all of that company's
    document types before any single document is labeled.
    """
    # text -> set of document types (not paths/timestamps) it appears in,
    # across this company's captures.
    text_to_types: dict = defaultdict(set)
    for path, blocks in blocks_by_path.items():
        doc_type = doc_type_by_path[path]
        for block in blocks:
            text_to_types[block.text].add(doc_type)

    rows = []
    for path, blocks in blocks_by_path.items():
        doc_type = doc_type_by_path[path]
        itemized_flags = mark_itemized_sections(blocks)
        doc_rows = 0
        for block, is_itemized in zip(blocks, itemized_flags):
            cross_type_repeats = len(text_to_types[block.text] - {doc_type})
            label, reason = weak_label(block, is_itemized, cross_type_repeats)
            if label is None:
                continue
            row = block_to_feature_dict(block)
            row["label"] = label
            row["label_reason"] = reason
            row["source"] = f"gcs:{path}"
            rows.append(row)
            doc_rows += 1
            if doc_rows >= MAX_BLOCKS_PER_DOC:
                break
    return rows


def synthetic_row(text: str, label: int, position_pct: float, itemized: bool = False) -> dict:
    ends_clause = text.rstrip().endswith((";", "."))
    return {
        "text": text,
        "link_density": 0.0,
        "char_len": len(text),
        "position_pct": position_pct,
        "is_boilerplate_ancestor": 1.0 if label == 1 else 0.0,
        "is_body_ancestor": 1.0 if label == 0 else 0.0,
        "word_count": float(len(text.split())),
        "has_link": 0.0,
        "ends_with_clause_punct": 1.0 if ends_clause else 0.0,
        "neighbor_link_density": 0.0,
        "label": label,
        "label_reason": "synthetic_itemized" if itemized else "synthetic",
        "source": "synthetic",
    }


def main():
    parser = argparse.ArgumentParser(
        description="Build a block-level labeled dataset for the boundary classifier."
    )
    parser.add_argument("--input", default="transparencydb_dev.documents_v2.json")
    parser.add_argument("--output", default="scripts/data/boundary_dataset.jsonl")
    parser.add_argument("--gcp-bucket", default="transparency_hub_documents")
    parser.add_argument("--limit-docs", type=int, default=None)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    from google.cloud.storage import Client

    docs = load_html_docs(Path(args.input), args.limit_docs)
    print(f"Found {len(docs)} documents with an html capture")

    client = Client()
    bucket = client.bucket(args.gcp_bucket)

    doc_type_by_path = {}
    company_by_path = {}
    for doc in docs:
        extension = doc["formats"]["html"]["extension"]
        path = f"{doc['path']}.{extension}"
        doc_type_by_path[path] = doc["type"]
        company_by_path[path] = doc["company_id"]

    blocks_by_path = {}
    t0 = time.time()
    print("Fetching and parsing HTML captures...")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(fetch_and_extract, doc, bucket) for doc in docs]
        for i, future in enumerate(as_completed(futures), 1):
            path, blocks = future.result()
            if blocks is not None:
                blocks_by_path[path] = blocks
            if i % 500 == 0:
                print(f"  ...{i}/{len(docs)} documents processed ({time.time() - t0:.0f}s)")

    print(
        f"Fetched+parsed {len(blocks_by_path)}/{len(docs)} documents in "
        f"{time.time() - t0:.0f}s"
    )

    paths_by_company: dict = defaultdict(list)
    for path in blocks_by_path:
        paths_by_company[company_by_path[path]].append(path)

    print(f"Labeling across {len(paths_by_company)} companies...")
    rows = []
    for company_id, paths in paths_by_company.items():
        company_blocks = {p: blocks_by_path[p] for p in paths}
        company_types = {p: doc_type_by_path[p] for p in paths}
        rows.extend(label_company(company_types, company_blocks))

    structural_count = len(rows)
    reason_counts: dict = defaultdict(int)
    for r in rows:
        reason_counts[r["label_reason"]] += 1
    print(f"Collected {structural_count} weakly-labeled blocks")
    for reason, count in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:24s} {count}")

    for text in SYNTHETIC_BOILERPLATE:
        rows.append(synthetic_row(text, label=1, position_pct=0.0))
    for text in SYNTHETIC_BODY:
        rows.append(synthetic_row(text, label=0, position_pct=0.5))
    for text in SYNTHETIC_ITEMIZED_BODY:
        rows.append(synthetic_row(text, label=0, position_pct=0.8, itemized=True))

    boilerplate_count = sum(1 for r in rows if r["label"] == 1)
    body_count = len(rows) - boilerplate_count
    print(f"Total dataset size: {len(rows)} ({boilerplate_count} boilerplate, {body_count} body)")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote dataset to {output_path}")


if __name__ == "__main__":
    main()
