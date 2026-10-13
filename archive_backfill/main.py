# Wayback Machine Historical Backfill - entry point
#
# Discovers and downloads historical snapshots of every company's policy
# document urls from the Wayback Machine, storing the ones that pass the
# same quality gates the live capture pipeline uses directly into the
# production documents_v2 collection -- so they show up in the Transparency
# Hub's Comparison Tool alongside live crawls, filling in each company's
# history further back than live crawling alone ever could.
#
# This is a separate GCP Cloud Run Job from the live pipeline's
# `document-capture` job (see the repo root's main.py/Dockerfile). It's
# intentionally slow (the Wayback Machine rate limits aggressively) and is
# meant to be triggered repeatedly -- e.g. nightly via Cloud Scheduler, the
# same way scheduler.ps1 triggers the live pipeline -- making a bounded
# amount of progress each run and picking back up where the last run left
# off. See archive_backfill/db.py for how that progress is tracked, and the
# repo README's "Wayback Backfill Job" section for deployment details.
#
# Usage:
#   python archive_backfill/main.py
#   python archive_backfill/main.py --dry_run true

import sys
from pathlib import Path

# Running this file directly (`python archive_backfill/main.py`, which is
# how the Dockerfile's ENTRYPOINT invokes it) puts THIS file's own directory
# on sys.path[0], not the repo root -- so `archive_backfill` itself (a
# package one level up from here) can't be found without this. Must happen
# before the `archive_backfill.*` imports below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archive_backfill import db, pipeline  # noqa: E402
from archive_backfill.settings import init_settings, logger  # noqa: E402


def main():
    init_settings()

    client = db.connect()
    cols = db.get_collections(client)
    db.ensure_indexes(cols)

    bucket = None
    from source.globals import CFG as ROOT_CFG

    if not ROOT_CFG.IN_CLOUD and ROOT_CFG.USE_GCP:
        from google.cloud.storage import Client

        logger.info("Connecting to Google Cloud Storage...")
        googleClient = Client()
        bucket = googleClient.bucket(ROOT_CFG.GCP_BUCKET)

    try:
        pipeline.run(cols, bucket)
    finally:
        client.close()

    logger.info("Wayback Machine backfill run finished, closing script...")
    sys.exit(0)


if __name__ == "__main__":
    main()
