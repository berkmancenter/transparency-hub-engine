# Transparency Archiver - Online Document Capture Tool
# Builds a labeled text dataset for training the policy-document classifier
# (see scripts/train_policy_classifier.py).
#
# Positive examples ("this is a policy document") come from the .txt capture
# of every document in a documents_v2-schema export that already has one --
# these have already made it through the capture pipeline's existing
# data_quality_check, and this project only ever archives policy-type pages
# (privacy policies, terms of service, community guidelines, etc.), so they
# are a reasonable stand-in for "confirmed policy document" ground truth.
#
# Negative examples ("this is NOT a policy document") don't exist anywhere in
# the database, since nothing non-policy is ever intentionally archived. This
# script synthesizes them by live-fetching pages that are almost certainly
# NOT policy documents:
#   - the homepage of every distinct domain seen among the positive examples
#     (same company, different and non-policy page)
#   - a small curated list of generic non-policy pages (news, wiki, docs,
#     product pages, etc.) for lexical diversity
#   - a handful of hand-written "bad capture" texts (cookie walls, 404 pages,
#     login walls, JS-required interstitials) since these are the realistic
#     failure mode the eventual snapshot-repair pipeline needs to catch: a
#     reconstructed .txt that is fluent English and long enough to pass
#     data_quality_check, but isn't actually the policy content.
#
# Usage:
#   python scripts/build_policy_dataset.py \
#       --input transparencydb_dev.documents_v2.json \
#       --output scripts/data/policy_dataset.jsonl

import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source.async_compare import read_text_from_html  # noqa: E402
from source.utils import decode_bytes  # noqa: E402

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Max characters kept per document. Keeps TF-IDF training fast and stops a
# handful of huge captures from dominating the vocabulary.
MAX_CHARS = 20000
MIN_CHARS = 50

# A small, stable set of pages that are almost certainly not policy
# documents, spanning several genres for lexical diversity beyond "homepage
# of a company already in our positive set".
GENERIC_NEGATIVE_URLS = [
    "https://en.wikipedia.org/wiki/Software_engineering",
    "https://en.wikipedia.org/wiki/Golden_Gate_Bridge",
    "https://en.wikipedia.org/wiki/Photosynthesis",
    "https://en.wikipedia.org/wiki/History_of_the_Internet",
    "https://en.wikipedia.org/wiki/Jazz",
    "https://en.wikipedia.org/wiki/Mount_Everest",
    "https://en.wikipedia.org/wiki/Renaissance",
    "https://en.wikipedia.org/wiki/Coffee",
    "https://www.bbc.com/news",
    "https://www.nytimes.com/section/technology",
    "https://www.espn.com/",
    "https://www.nationalgeographic.com/animals",
    "https://www.bbcgoodfood.com/recipes/spaghetti-carbonara",
    "https://docs.python.org/3/tutorial/index.html",
    "https://en.wikipedia.org/wiki/Special:Random",
    "https://en.wikipedia.org/wiki/Special:Random",
    "https://en.wikipedia.org/wiki/Special:Random",
    "https://en.wikipedia.org/wiki/Special:Random",
    "https://en.wikipedia.org/wiki/Special:Random",
    "https://en.wikipedia.org/wiki/Special:Random",
]

# Hand-written "bad capture" texts -- the realistic failure mode of the
# snapshot-repair pipeline: fluent, long-enough-to-pass-quality-check English
# that is nonetheless not the actual policy content.
SYNTHETIC_NEGATIVES = [
    (
        "We use cookies and similar technologies to help personalize content, "
        "tailor and measure ads, and provide a better experience. By clicking "
        "accept, you agree to this use of cookies as described in our Cookie "
        "Policy. You can manage your preferences at any time by visiting the "
        "settings menu. Some cookies are necessary for the website to "
        "function and cannot be switched off. Others are used to improve "
        "performance and your browsing experience."
    ),
    (
        "404 Error Page Not Found. The page you are looking for might have "
        "been removed, had its name changed, or is temporarily unavailable. "
        "Please check the URL and try again, return to the homepage, or use "
        "the search bar above to find what you were looking for. If you "
        "believe this is an error, please contact our support team for "
        "further assistance."
    ),
    (
        "Please enable JavaScript to continue using this application. Your "
        "browser has JavaScript disabled, which prevents this page from "
        "loading correctly. To view this content, please enable JavaScript "
        "in your browser settings and refresh the page. If you continue to "
        "experience issues after enabling JavaScript, try clearing your "
        "browser cache or using a different browser."
    ),
    (
        "You must sign in to continue. Please enter your email address and "
        "password to access your account. If you don't have an account yet, "
        "you can create one for free in just a few minutes. Forgot your "
        "password? Click the link below to reset it. For security reasons, "
        "we periodically ask you to verify your identity before granting "
        "access to sensitive account information."
    ),
    (
        "We're currently performing scheduled maintenance to improve our "
        "services. We expect to be back online shortly. Thank you for your "
        "patience while we work on making things better for you. If this "
        "downtime is affecting your business, please reach out to our "
        "support team and we will do our best to assist you as quickly as "
        "possible."
    ),
    (
        "Verify you are human by completing the action below. This "
        "additional step helps us keep our platform safe from automated "
        "traffic and abuse. Please complete the challenge to continue to "
        "your destination. If you are having trouble completing this "
        "verification, try refreshing the page or disabling any browser "
        "extensions that might interfere with the check."
    ),
    (
        "Welcome to our store! Shop the latest arrivals, best sellers, and "
        "seasonal deals all in one place. Free shipping on orders over fifty "
        "dollars. Sign up for our newsletter to get ten percent off your "
        "first purchase. Follow us on social media for exclusive discounts, "
        "styling tips, and behind-the-scenes content from our design team."
    ),
    (
        "Breaking news from around the world, updated around the clock. Our "
        "reporters bring you the latest on politics, business, technology, "
        "and culture. Subscribe today to get unlimited access to our full "
        "archive of articles, exclusive newsletters, and ad-free reading "
        "across all of your devices."
    ),
]


def load_positive_docs(input_path: Path, limit: int | None) -> list[dict]:
    with open(input_path, "r", encoding="utf-8") as f:
        docs = json.load(f)
    with_txt = [d for d in docs if "txt" in d.get("formats", {})]
    if limit is not None:
        with_txt = with_txt[:limit]
    return with_txt


def distinct_homepage_urls(docs: list[dict]) -> list[str]:
    domains = set()
    for doc in docs:
        original_url = doc.get("formats", {}).get("txt", {}).get("original_url", "")
        parsed = urlparse(original_url)
        if parsed.scheme and parsed.netloc:
            domains.add(f"{parsed.scheme}://{parsed.netloc}/")
    return sorted(domains)


def fetch_gcs_text(doc: dict, bucket) -> tuple[str, str] | None:
    extension = doc["formats"]["txt"]["extension"]
    gcs_path = f"{doc['path']}.{extension}"
    try:
        blob = bucket.blob(gcs_path)
        raw_bytes = blob.download_as_bytes(timeout=60)
        text = decode_bytes(raw_bytes)
        return text, f"gcs:{gcs_path}"
    except Exception as e:
        print(f"  Failed to fetch {gcs_path}: {e}")
        return None


def fetch_web_text(url: str) -> tuple[str, str] | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_bytes = resp.read()
        text = read_text_from_html(raw_bytes)
        return text, f"web:{url}"
    except Exception as e:
        print(f"  Failed to fetch {url}: {e}")
        return None


def clean(text: str) -> str | None:
    text = " ".join(text.split())
    if len(text) < MIN_CHARS:
        return None
    return text[:MAX_CHARS]


def main():
    parser = argparse.ArgumentParser(
        description="Build a labeled dataset for the policy-document classifier."
    )
    parser.add_argument("--input", default="transparencydb_dev.documents_v2.json")
    parser.add_argument("--output", default="scripts/data/policy_dataset.jsonl")
    parser.add_argument("--gcp-bucket", default="transparency_hub_documents")
    parser.add_argument("--limit-positive", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    from google.cloud.storage import Client

    positive_docs = load_positive_docs(Path(args.input), args.limit_positive)
    print(f"Found {len(positive_docs)} positive (txt-capture) candidates")

    homepage_urls = distinct_homepage_urls(positive_docs)
    print(f"Found {len(homepage_urls)} distinct company homepages for negatives")

    negative_urls = homepage_urls + GENERIC_NEGATIVE_URLS

    client = Client()
    bucket = client.bucket(args.gcp_bucket)

    rows = []
    t0 = time.time()

    print("Fetching positive examples from GCS...")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(fetch_gcs_text, doc, bucket): doc for doc in positive_docs
        }
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                text, source = result
                text = clean(text)
                if text:
                    doc = futures[future]
                    rows.append(
                        {
                            "text": text,
                            "label": 1,
                            "source": source,
                            "company_id": doc.get("company_id"),
                            "type": doc.get("type"),
                        }
                    )
            if i % 250 == 0:
                print(f"  ...{i}/{len(positive_docs)} positive fetches done")

    positive_count = len(rows)
    print(f"Collected {positive_count} positive examples")

    print(f"Fetching {len(negative_urls)} candidate negative pages...")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_web_text, url): url for url in negative_urls}
        for future in as_completed(futures):
            result = future.result()
            if result:
                text, source = result
                text = clean(text)
                if text:
                    rows.append({"text": text, "label": 0, "source": source})

    for text in SYNTHETIC_NEGATIVES:
        rows.append({"text": text, "label": 0, "source": "synthetic"})

    negative_count = len(rows) - positive_count
    print(f"Collected {negative_count} negative examples")
    print(f"Total dataset size: {len(rows)} ({time.time() - t0:.1f}s)")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote dataset to {output_path}")


if __name__ == "__main__":
    main()
