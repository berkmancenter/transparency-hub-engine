# Wayback Machine Historical Backfill - Job Configuration
# Mirrors the config pattern in source/globals.py (env vars + a yaml file,
# Cloud Run detection, a dataclass singleton) but is intentionally a
# self-contained copy rather than a shared import -- this job is meant to be
# containerized and deployed independently of the live capture pipeline (see
# archive_backfill/Dockerfile), so its config surface shouldn't be coupled to
# main.py's argparse flags or config.yaml schema.
#
# This module ALSO bridges the settings that matter into the root
# source/globals.py CFG singleton (see bridge_root_cfg below). That's not
# optional: this job reuses several functions from source/ as-is
# (source.async_compare's text reconstruction/quality-gate/fuzzy-diff
# helpers, source.changes.create_changes_json, source.utils.mongo_connect /
# get_gcs_bytes / notify_slack) and every one of them reads its settings from
# the root CFG object rather than taking them as arguments. Rather than fork
# that logic, we run it in-process with the root CFG populated to match this
# job's settings.

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

# This job's own .env.local / .env, loaded before the repo root's, so a
# value declared here always takes precedence over a same-named root value.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
load_dotenv(dotenv_path=_THIS_DIR / ".env")
load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=False)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("archive_backfill")


@dataclass
class BackfillConfig:
    DRY_RUN: bool = True
    IN_LOCAL: bool = True
    IN_CLOUD: bool = False
    IN_DOCKER: bool = False

    # Mongo. MAIN_DB_NAME is the production database this job reads
    # companies from and writes backfilled snapshots into (documents_v2) --
    # per-user decision, backfilled captures live directly alongside live
    # crawls so they show up in the Comparison Tool immediately.
    # BACKFILL_DB_NAME is a *separate* database (same cluster) used only to
    # track this job's own step-by-step progress; it never holds document
    # content, only status/bookkeeping records.
    MONGODB_URL: str = "mongodb://localhost:27017/"
    MAIN_DB_NAME: str = "dev_db"
    BACKFILL_DB_NAME: str = "archive_backfill"

    USE_GCP: bool = True
    GCP_BUCKET: str = "my-bucket-1234"
    GCLOUD_PROJECT: str = "my-project-1234"
    GCLOUD_REGION: str = "us-east4"
    # Only meaningful when IN_CLOUD: the root of the GCS FUSE volume mount,
    # matching how the live pipeline's CAPTURES_DIR works (see README.md
    # "Running on Google Cloud Platform").
    CAPTURES_DIR: Path = Path("/mnt")

    # CDX discovery window -- the Wayback Machine's earliest crawls are from
    # 1997; END_YEAR should be bumped periodically as more history accrues.
    START_YEAR: int = 1997
    END_YEAR: int = 2025

    # Rate limiting. The CDX API and the raw snapshot fetch are rate limited
    # independently by web.archive.org, hence two separate sleep/backoff
    # pairs. See archive_backfill/cdx.py and archive_backfill/fetch.py.
    CDX_SLEEP_SECONDS: float = 2.0
    CDX_BACKOFF_SECONDS: float = 30.0
    FETCH_SLEEP_SECONDS: float = 1.0
    FETCH_BACKOFF_SECONDS: float = 15.0

    # Retry bookkeeping for both phases (see archive_backfill/db.py). A work
    # item that keeps failing is retried up to MAX_RETRIES times, waiting at
    # least RETRY_BACKOFF_MINUTES between attempts, then left in a
    # permanently "failed" state for manual review instead of retried forever.
    MAX_RETRIES: int = 5
    RETRY_BACKOFF_MINUTES: int = 30

    # How much work a single job execution takes on before exiting cleanly.
    # 0 means "no limit" for that axis. Progress is checkpointed continuously
    # in Mongo (see archive_backfill/db.py), so a run that hits any of these
    # limits -- or is killed by Cloud Run's task timeout -- picks back up
    # exactly where it left off on the next scheduled execution. This is the
    # cloud-native replacement for the original script's local
    # web_archive_links.json/web_archive_failed.json checkpoint files, which
    # don't survive Cloud Run's ephemeral filesystem between executions.
    TIME_BUDGET_SECONDS: int = 0
    DISCOVERY_BATCH_LIMIT: int = 0
    FETCH_BATCH_LIMIT: int = 0
    # How many companies one run works through (see pipeline.run_focused);
    # 0 = no per-company focus, batch limits apply across all companies
    COMPANIES_PER_RUN: int = 1

    # Reused as-is by source/async_compare.py's has_content_changed /
    # source/policy_classifier.py's is_policy_document -- see bridge_root_cfg.
    FUZZY_THRESHOLD: int = 94
    USE_POLICY_CLASSIFIER: bool = True
    POLICY_CLASSIFIER_THRESHOLD: float = 0.3
    MAX_WORKERS: int = 4

    CREATE_CHANGES: bool = True
    USE_SLACK: bool = False
    SLACK_WEBHOOK_URL: str = ""

    LOG_DIR: Path = Path("logs")


CFG = BackfillConfig()


def str_to_bool(value: str) -> bool:
    return str(value).lower() in ("true", "1", "yes", "t", "y")


def init_settings() -> BackfillConfig:
    """
    Populates CFG from (in increasing priority) archive_backfill/config.yaml,
    environment variables, and CLI flags, detects the runtime environment,
    and sets up file logging. Mirrors source/globals.py's init_globals, but
    scoped to this job's own settings and config file.
    """
    if os.getenv("CLOUD_RUN_EXECUTION") is not None:
        CFG.IN_CLOUD = True
        CFG.IN_LOCAL = False
        CFG.DRY_RUN = False
    elif os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true":
        CFG.IN_DOCKER = True
        CFG.IN_LOCAL = False

    parser = argparse.ArgumentParser(
        description="Wayback Machine Historical Backfill Job"
    )
    parser.add_argument(
        "--dry_run",
        type=str_to_bool,
        default=os.getenv("DRY_RUN", "false").lower() == "true",
        help="Discover/fetch/log everything but never write to Mongo or GCS",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=os.getenv("BACKFILL_CONFIG_PATH", str(_THIS_DIR / "config.yaml")),
        help="Path to this job's config.yaml",
    )
    parser.add_argument(
        "--mongodb_url",
        type=str,
        default=os.getenv("MONGODB_URL", "mongodb://localhost:27017/"),
    )
    parser.add_argument(
        "--gcp_bucket",
        type=str,
        default=os.getenv("GCP_BUCKET", "my-bucket-1234"),
    )
    parser.add_argument(
        "--gcloud_project",
        type=str,
        default=os.getenv("GCLOUD_PROJECT", "my-project-1234"),
    )
    parser.add_argument(
        "--captures_dir",
        type=str,
        default=os.getenv("CAPTURES_DIR", "/mnt"),
        help="Root of the GCS FUSE mount, only used when running on Cloud Run",
    )
    parser.add_argument(
        "--log_dir",
        type=str,
        default=os.getenv("LOG_DIR", "logs"),
    )
    args, _unknown = parser.parse_known_args()

    CFG.DRY_RUN = args.dry_run if not CFG.IN_CLOUD else False
    CFG.MONGODB_URL = args.mongodb_url
    CFG.GCP_BUCKET = args.gcp_bucket
    CFG.GCLOUD_PROJECT = args.gcloud_project
    CFG.CAPTURES_DIR = Path(args.captures_dir)
    CFG.LOG_DIR = Path(args.log_dir)

    config_path = Path(args.config)
    try:
        with open(config_path, "r") as f:
            config: dict = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"No config file found at {config_path}, using defaults")
        config = {}
    except Exception as e:
        logger.error(f"Failed to load config file {config_path}: {e}")
        sys.exit(1)

    CFG.MAIN_DB_NAME = config.get("MAIN_DB_NAME", "dev_db")
    CFG.BACKFILL_DB_NAME = config.get("BACKFILL_DB_NAME", "archive_backfill")
    CFG.USE_GCP = False if CFG.IN_CLOUD else config.get("USE_GCP", True)
    CFG.START_YEAR = config.get("START_YEAR", 1997)
    CFG.END_YEAR = config.get("END_YEAR", 2025)
    CFG.CDX_SLEEP_SECONDS = config.get("CDX_SLEEP_SECONDS", 2.0)
    CFG.CDX_BACKOFF_SECONDS = config.get("CDX_BACKOFF_SECONDS", 30.0)
    CFG.FETCH_SLEEP_SECONDS = config.get("FETCH_SLEEP_SECONDS", 1.0)
    CFG.FETCH_BACKOFF_SECONDS = config.get("FETCH_BACKOFF_SECONDS", 15.0)
    CFG.MAX_RETRIES = config.get("MAX_RETRIES", 5)
    CFG.RETRY_BACKOFF_MINUTES = config.get("RETRY_BACKOFF_MINUTES", 30)
    CFG.TIME_BUDGET_SECONDS = config.get("TIME_BUDGET_SECONDS", 0)
    CFG.DISCOVERY_BATCH_LIMIT = config.get("DISCOVERY_BATCH_LIMIT", 0)
    CFG.FETCH_BATCH_LIMIT = config.get("FETCH_BATCH_LIMIT", 0)
    CFG.COMPANIES_PER_RUN = config.get("COMPANIES_PER_RUN", 1)
    CFG.FUZZY_THRESHOLD = config.get("FUZZY_THRESHOLD", 94)
    CFG.USE_POLICY_CLASSIFIER = config.get("USE_POLICY_CLASSIFIER", True)
    CFG.POLICY_CLASSIFIER_THRESHOLD = config.get("POLICY_CLASSIFIER_THRESHOLD", 0.3)
    CFG.MAX_WORKERS = config.get("MAX_WORKERS", 4)
    CFG.CREATE_CHANGES = config.get("CREATE_CHANGES", True)
    CFG.USE_SLACK = config.get("USE_SLACK", False)
    CFG.SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

    os.makedirs(CFG.LOG_DIR, exist_ok=True)
    log_file = Path(
        CFG.LOG_DIR, f"archive_backfill_{time.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
    )
    fileHandler = logging.FileHandler(log_file)
    fileHandler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(fileHandler)

    if CFG.USE_GCP and CFG.GCP_BUCKET == "my-bucket-1234":
        logger.warning("Using the default GCP bucket placeholder -- set GCP_BUCKET")

    logger.info(
        f"""Starting Wayback Machine Historical Backfill with the following options:

        {"Running in Cloud Run" if CFG.IN_CLOUD else "Running in Docker" if CFG.IN_DOCKER else "Running Locally"}
        DRY_RUN = {CFG.DRY_RUN}
        MAIN_DB_NAME = {CFG.MAIN_DB_NAME}
        BACKFILL_DB_NAME = {CFG.BACKFILL_DB_NAME}
        USE_GCP = {CFG.USE_GCP}
        GCP_BUCKET = {CFG.GCP_BUCKET}
        START_YEAR-END_YEAR = {CFG.START_YEAR}-{CFG.END_YEAR}
        TIME_BUDGET_SECONDS = {CFG.TIME_BUDGET_SECONDS or "unlimited"}
        MAX_RETRIES = {CFG.MAX_RETRIES}
        USE_POLICY_CLASSIFIER = {CFG.USE_POLICY_CLASSIFIER}
        CREATE_CHANGES = {CFG.CREATE_CHANGES}
        """
    )

    bridge_root_cfg()
    return CFG


def bridge_root_cfg():
    """
    Populates the root source/globals.py CFG singleton with the settings
    this job needs, WITHOUT calling its init_globals() -- that function runs
    its own argparse pass (which would collide with this job's own CLI
    flags), creates directories this job doesn't use, and expects the live
    pipeline's config.yaml/​.env layout. Instead we import the already
    lazily-initialized default CFG object and set only the fields the
    source/ helpers this job reuses actually read:

      - source.utils.mongo_connect / get_gcs_bytes / notify_slack
      - source.async_compare.has_content_changed / passes_policy_document_check
      - source.policy_classifier.is_policy_document
      - source.changes.create_changes_json

    This must run after CFG (this module's BackfillConfig) is fully
    populated, and before any of the source/ helpers above are called.
    """
    sys.path.insert(0, str(_REPO_ROOT))
    from source.globals import CFG as ROOT_CFG

    ROOT_CFG.DRY_RUN = CFG.DRY_RUN
    ROOT_CFG.USE_MONGO = True
    ROOT_CFG.MONGODB_URL = CFG.MONGODB_URL
    ROOT_CFG.DB_NAME = CFG.MAIN_DB_NAME
    ROOT_CFG.USE_GCP = CFG.USE_GCP
    ROOT_CFG.GCP_BUCKET = CFG.GCP_BUCKET
    ROOT_CFG.GCLOUD_PROJECT = CFG.GCLOUD_PROJECT
    ROOT_CFG.IN_CLOUD = CFG.IN_CLOUD
    ROOT_CFG.CAPTURES_DIR = CFG.CAPTURES_DIR
    ROOT_CFG.FUZZY_THRESHOLD = CFG.FUZZY_THRESHOLD
    ROOT_CFG.USE_POLICY_CLASSIFIER = CFG.USE_POLICY_CLASSIFIER
    ROOT_CFG.POLICY_CLASSIFIER_THRESHOLD = CFG.POLICY_CLASSIFIER_THRESHOLD
    ROOT_CFG.MAX_WORKERS = CFG.MAX_WORKERS
    ROOT_CFG.USE_SLACK = CFG.USE_SLACK
    ROOT_CFG.SLACK_WEBHOOK_URL = CFG.SLACK_WEBHOOK_URL
    # Boundary/diff-trimmer is purely cosmetic (see source/globals.py) and
    # unrelated to this job's own quality gating -- leave it at the root's
    # own default rather than forcing a value here.
