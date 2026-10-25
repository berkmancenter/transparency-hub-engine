# Transparency Archiver - Online Document Capture Tool
# This file handles command line arguments, environment variables, and config values for the Capture functions

# Imports
import argparse
import os
import re
import logging
import yaml
from pathlib import Path
from dotenv import load_dotenv
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

# Load environment variables from .env
load_dotenv(dotenv_path=".env")

# Set logging configuration to be used across the Capture Tool
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# Initialize all global variables
@dataclass
class AppConfig:
    DRY_RUN: bool = True
    # Directories to use during runtime
    TEMP_DIR: Path = Path("tmp")
    CRAWL_DIR: Path = Path("tmp")
    DOCS_DIR: Path = Path("tmp/docs")
    CAPTURES_DIR: Path = Path("tmp/docs")
    CHANGES_DIR: Path = Path("changes")
    LOG_DIR: Path = Path("logs")
    LOG_FILE: Path = Path("logs/default_log.txt")
    RECOVERY_DATA: list = field(default_factory=list)
    # External Connections
    USE_MONGO: bool = True
    MONGODB_URL: str = "mongodb://localhost:27017/"
    DB_NAME: str = "dev_db"
    DOCUMENTS_BSON: Path = Path("db/documents.bson")
    CHANGES_BSON: Path = Path("db/changes.bson")
    USE_GCP: bool = True
    GCP_BUCKET: str = "my-bucket-1234"
    GCLOUD_PROJECT: str = "my-project-1234"
    GCLOUD_REGION: str = "us-east4"
    GCLOUD_JOB: str = "my-job-1234"
    USE_LOCAL_COMPANIES: bool = False
    COMPANIES_JSON: Path = Path("db/companies.json")
    # Configuration Settings
    SYNC_PRIORITY: int = 1
    # Upper bound on the priorities a run captures; 0 = no upper bound
    SYNC_PRIORITY_MAX: int = 0
    # Priority for companies without a PRIORITY_DICT entry
    DEFAULT_PRIORITY: int = 1
    FUZZY_THRESHOLD: int = 97
    MAX_WORKERS: int = 2
    TIMEOUT: int = 1200
    CREATE_CHANGES: bool = True
    TOPIC_PATTERNS: dict = field(default_factory=dict)
    PRIORITY_DICT: dict = field(default_factory=dict)
    # Policy-document classifier (source/policy_classifier.py)
    USE_POLICY_CLASSIFIER: bool = True
    POLICY_CLASSIFIER_THRESHOLD: float = 0.3
    # Header/footer boundary trimmer (source/boundary_classifier.py). Cosmetic
    # only: trims boilerplate out of the diff written to .CHANGES.json for
    # display in the Comparison Tool. Never affects whether a capture is
    # saved or a change is detected -- see source/changes.py.
    USE_DIFF_TRIMMER: bool = False
    DIFF_TRIMMER_THRESHOLD: float = 0.85
    # Slack Notifications
    USE_SLACK: bool = False
    SLACK_WEBHOOK_URL: str = ""
    # Runtime memory safeguard: self-imposed address-space cap, set below the
    # container's actual memory limit so a single oversized document raises a
    # catchable MemoryError instead of the OS OOM-killing the whole process.
    # 0 disables the cap (default, and always disabled outside POSIX).
    MEMORY_LIMIT_MB: int = 0
    MEMORY_SOFT_LIMIT_RATIO: float = 0.85
    # Program environment management
    IN_LOCAL: bool = True
    IN_CLOUD: bool = False
    IN_DOCKER: bool = False


CFG = AppConfig()


def str_to_bool(value: str) -> bool:
    """
    Parses a CLI string argument into a bool. `argparse`'s built-in `type=bool`
    just calls `bool()` on the raw string, so any non-empty value (including
    "false") evaluates to True; this treats common truthy/falsy strings as
    expected instead.
    """
    return value.lower() in ("true", "1", "yes", "t", "y")


def apply_memory_safeguard():
    """
    Self-imposes a virtual address-space cap (RLIMIT_AS) below the
    container's actual memory limit, when CFG.MEMORY_LIMIT_MB is configured.

    Without this, a single oversized document (e.g. a large HTML DOM parse
    or WACZ decompression during .txt reconstruction) can push the process
    RSS up to the Cloud Run container's hard limit, at which point the Linux
    OOM killer sends an uncatchable SIGKILL -- terminating every in-flight
    worker and losing the run. With the cap in place, Python's own allocator
    raises a normal MemoryError once the soft limit is crossed, which the
    ThreadPoolExecutor's per-future exception handling can catch and log as
    a single failed document instead of a killed container.

    RLIMIT_AS is POSIX-only and is a no-op on Windows (local dev). It bounds
    virtual address space rather than RSS directly, so it's a proxy for
    actual memory use rather than an exact match -- fine here since nothing
    in this pipeline reserves large address ranges without using them.
    """
    if CFG.MEMORY_LIMIT_MB <= 0:
        return
    try:
        import resource  # type: ignore
    except ImportError:
        logger.info(
            "Skipping memory safeguard: RLIMIT_AS is not available on this platform"
        )
        return

    limit_bytes = int(CFG.MEMORY_LIMIT_MB * 1024 * 1024 * CFG.MEMORY_SOFT_LIMIT_RATIO)
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)  # type: ignore
        if hard != resource.RLIM_INFINITY:  # type: ignore
            limit_bytes = min(limit_bytes, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, hard))  # type: ignore
        logger.info(
            f"Applied memory safeguard: RLIMIT_AS set to {limit_bytes / (1024 * 1024):.0f}MB "
            f"({CFG.MEMORY_SOFT_LIMIT_RATIO:.0%} of configured {CFG.MEMORY_LIMIT_MB}MB container limit)"
        )
    except (ValueError, OSError) as e:
        logger.warning(f"Failed to apply memory safeguard, continuing without it: {e}")


def init_globals():
    # Check if this is running locally or on Google Cloud Run
    # As per: https://docs.cloud.google.com/run/docs/container-contract
    # Google Cloud Run will set the CLOUD_RUN_EXECUTION environment variable
    if os.getenv("CLOUD_RUN_EXECUTION") is not None:
        CFG.DRY_RUN = False
        CFG.IN_CLOUD = True
        CFG.IN_LOCAL = False
    # Defer to a user set flag for Docker detection
    # This is set in the Dockerfile, but not in the default local env file
    elif os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true":
        CFG.IN_DOCKER = True
        CFG.IN_LOCAL = False

    # Verify that an environment was detected
    if not (CFG.IN_LOCAL or CFG.IN_CLOUD or CFG.IN_DOCKER):
        logger.error("Could not detect environment, exiting")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Transparency Archiver - Online Document Capture Tool"
    )

    parser.add_argument(
        "--dry_run",
        type=str_to_bool,
        default=os.getenv("DRY_RUN", "false").lower() == "true",
        help="Conduct a dry run, will not save outputs to external connections, will not delete temporary files from runtime (default: false)",
    )

    parser.add_argument(
        "--temp_dir",
        type=str,
        default=os.getenv("TEMP_DIR", "tmp"),
        help="Temporary directory for downloads and processing (default: tmp)",
    )

    parser.add_argument(
        "--captures_dir",
        type=str,
        default=os.getenv("CAPTURES_DIR", "tmp/docs"),
        help="Directory to store the captures (default: tmp/docs)",
    )

    parser.add_argument(
        "--changes_dir",
        type=str,
        default=os.getenv("CHANGES_DIR", "changes"),
        help="Directory to store the changes documents, these will not be created if --dry_run is set to true (default: changes)",
    )

    parser.add_argument(
        "--log_dir",
        type=str,
        default=os.getenv("LOG_DIR", "logs"),
        help="Directory to store the log files and recovery file (default: logs)",
    )

    parser.add_argument(
        "--mongodb_url",
        type=str,
        default=os.getenv("MONGODB_URL", "mongodb://localhost:27017/"),
        help="MongoDB connection string (default: mongodb://localhost:27017/)",
    )

    parser.add_argument(
        "--gcp_bucket",
        type=str,
        default=os.getenv("GCP_BUCKET", "my-bucket-1234"),
        help="GCP bucket name (default: my-bucket-1234)",
    )

    parser.add_argument(
        "--gcloud_project",
        type=str,
        default=os.getenv("GCLOUD_PROJECT", "my-project-1234"),
        help="GCloud project name (default: my-project-1234)",
    )

    parser.add_argument(
        "--gcloud_region",
        type=str,
        default=os.getenv("GCLOUD_REGION", "us-east4"),
        help="GCloud project region (default: us-east4)",
    )

    parser.add_argument(
        "--gcloud_job",
        type=str,
        default=os.getenv("GCLOUD_JOB", "my-job-1234"),
        help="GCloud job identifier for browsertrix fork (default: my-job-1234)",
    )

    parser.add_argument(
        "--sync_priority",
        type=int,
        default=int(os.getenv("SYNC_PRIORITY", 1)),
        help="Priority for syncing (default: 1) When set to 1, will run on all companies not disabled in the config",
    )

    parser.add_argument(
        "--sync_priority_max",
        type=int,
        default=int(os.getenv("SYNC_PRIORITY_MAX", 0)),
        help="Highest priority to sync (default: 0, no upper bound). E.g. --sync_priority 1 --sync_priority_max 1 syncs only the priority-1 (monthly) tier",
    )

    parser.add_argument(
        "--config",
        type=str,
        default=os.getenv("CONFIG_PATH", "config.yaml"),
        help="Path to configuration file (default: config.yaml)",
    )

    args = parser.parse_args()

    CFG.DRY_RUN = args.dry_run
    CFG.TEMP_DIR = Path(args.temp_dir)
    CFG.CRAWL_DIR = Path(args.temp_dir)
    CFG.DOCS_DIR = Path(args.temp_dir, "docs")
    CFG.CAPTURES_DIR = Path(args.captures_dir)
    CFG.CHANGES_DIR = Path(args.changes_dir)
    CFG.LOG_DIR = Path(args.log_dir)
    CFG.MONGODB_URL = args.mongodb_url
    CFG.GCP_BUCKET = args.gcp_bucket
    CFG.GCLOUD_PROJECT = args.gcloud_project
    CFG.GCLOUD_REGION = args.gcloud_region
    CFG.GCLOUD_JOB = args.gcloud_job
    CFG.SYNC_PRIORITY = args.sync_priority
    CFG.SYNC_PRIORITY_MAX = args.sync_priority_max
    # Not a global variable, only relevant for this module
    CONFIG_FILE = args.config

    try:
        with open(CONFIG_FILE, "r") as f:
            config: dict = yaml.safe_load(f)

        CFG.USE_LOCAL_COMPANIES = config.get("USE_LOCAL_COMPANIES", False)
        CFG.COMPANIES_JSON = Path(config.get("COMPANIES_JSON", "db/companies.json"))
        CFG.FUZZY_THRESHOLD = config.get("FUZZY_RATIO", 97)

        CFG.USE_MONGO = config.get("USE_MONGO", True)
        CFG.DB_NAME = config.get("DB_NAME", "dev_db")
        CFG.DOCUMENTS_BSON = Path(config.get("DOCUMENTS_BSON", "db/documents.bson"))
        CFG.CHANGES_BSON = Path(config.get("CHANGES_BSON", "db/changes.bson"))
        # USE_GCP must be False if running in Google Cloud Run
        CFG.USE_GCP = False if CFG.IN_CLOUD else config.get("USE_GCP", True)
        CFG.MAX_WORKERS = config.get("MAX_WORKERS", 4)
        CFG.TIMEOUT = config.get("TIMEOUT", 1200)
        CFG.CREATE_CHANGES = config.get("CREATE_CHANGES", True)
        CFG.TOPIC_PATTERNS = {
            topic: [re.compile(p, re.IGNORECASE) for p in patterns]
            for topic, patterns in config.get("TOPIC_PATTERNS", {}).items()
        }
        CFG.PRIORITY_DICT = config.get("PRIORITY_DICT") or {}
        CFG.DEFAULT_PRIORITY = config.get("DEFAULT_PRIORITY", 1)
        CFG.USE_POLICY_CLASSIFIER = config.get("USE_POLICY_CLASSIFIER", True)
        CFG.POLICY_CLASSIFIER_THRESHOLD = config.get("POLICY_CLASSIFIER_THRESHOLD", 0.3)
        CFG.USE_DIFF_TRIMMER = config.get("USE_DIFF_TRIMMER", False)
        CFG.DIFF_TRIMMER_THRESHOLD = config.get("DIFF_TRIMMER_THRESHOLD", 0.85)
        CFG.USE_SLACK = config.get("USE_SLACK", False)
        CFG.SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
        CFG.MEMORY_LIMIT_MB = config.get("MEMORY_LIMIT_MB", 0)
        CFG.MEMORY_SOFT_LIMIT_RATIO = config.get("MEMORY_SOFT_LIMIT_RATIO", 0.85)
    except Exception as e:
        logger.error(f"Loading config failed, check {CONFIG_FILE}: {e}")
        sys.exit(1)

    # Create a custom Log file based on the time this job started
    os.makedirs(CFG.LOG_DIR, exist_ok=True)
    CFG.LOG_FILE = Path(CFG.LOG_DIR, f"log_{time.strftime('%Y-%m-%d_%H-%M-%S')}.txt")
    fileHandler = logging.FileHandler(CFG.LOG_FILE)
    fileHandler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(fileHandler)

    # Check for recovery data from a previous failed run
    try:
        with open(CFG.LOG_DIR / "recovery_log.txt", "r") as f:
            recovery: list = f.read().splitlines()
        CFG.RECOVERY_DATA = recovery
    except FileNotFoundError as e:
        logger.error(f"No recovery file found, assuming fresh start: {e}")
    except Exception as e:
        logger.error(
            f"Loading recovery file failed, check {CFG.LOG_DIR / 'recovery_log.txt'}: {e}"
        )
        sys.exit(1)

    # Create any local directories needed for the script
    # By default all of these exist in the TEMP_DIR, which will be cleaned up at the end of the script
    os.makedirs(CFG.TEMP_DIR, exist_ok=True)
    os.makedirs(CFG.DOCS_DIR, exist_ok=True)
    os.makedirs(CFG.CRAWL_DIR, exist_ok=True)
    os.makedirs(CFG.CAPTURES_DIR, exist_ok=True)
    os.makedirs(CFG.CAPTURES_DIR / CFG.CHANGES_DIR, exist_ok=True)
    # Make sure the database directories exist in the event that this is a first run
    if not CFG.USE_MONGO:
        os.makedirs(CFG.DOCUMENTS_BSON.parent, exist_ok=True)
        os.makedirs(CFG.CHANGES_BSON.parent, exist_ok=True)

    apply_memory_safeguard()

    if CFG.USE_GCP:
        if CFG.GCP_BUCKET == "my-bucket-1234":
            logger.warning(
                "Warning, you are using the default gcp bucket.  Set GCP_BUCKET in .env to use a different bucket."
            )
        if CFG.GCLOUD_PROJECT == "my-project-1234":
            logger.warning(
                "Warning, you are using the default gcloud project.  Set GCLOUD_PROJECT in .env to use a different bucket."
            )

    # Log the options after they've been parsed
    options_string = f"""Starting Transparency Archiver - Online Document Capture Tool with the following options:
          
          {"Running in Local Mode" if CFG.IN_LOCAL else "Running in Docker Mode" if CFG.IN_DOCKER else "Running in Google Cloud Mode" if CFG.IN_CLOUD else "Could not detect environment"}
          DRY_RUN = {CFG.DRY_RUN}
          TEMP_DIR = {CFG.TEMP_DIR}
          CRAWL_DIR = {CFG.CRAWL_DIR}
          DOCS_DIR = {CFG.DOCS_DIR}
          CAPTURES_DIR = {CFG.CAPTURES_DIR}
          LOG_FILE = {CFG.LOG_FILE}
          USE_MONGO = {CFG.USE_MONGO}
          MONGODB_URL = SECRET
          {"DOCUMENTS_BSON = " + str(CFG.DOCUMENTS_BSON) if not CFG.USE_MONGO else "None"}
          {"CHANGES_BSON = " + str(CFG.CHANGES_BSON) if not CFG.USE_MONGO else "None"}
          USE_GCP = {CFG.USE_GCP}
          GCP_BUCKET = {CFG.GCP_BUCKET}
          GCLOUD_PROJECT = {CFG.GCLOUD_PROJECT}
          GCLOUD_REGION = {CFG.GCLOUD_REGION}
          GCLOUD_JOB = {CFG.GCLOUD_JOB}
          SYNC_PRIORITY = {CFG.SYNC_PRIORITY}
          SYNC_PRIORITY_MAX = {CFG.SYNC_PRIORITY_MAX or "none"}
          DEFAULT_PRIORITY = {CFG.DEFAULT_PRIORITY}
          CONFIG_FILE = {CONFIG_FILE}
          USE_LOCAL_COMPANIES = {CFG.USE_LOCAL_COMPANIES}
          {"COMPANIES_JSON = " + str(CFG.COMPANIES_JSON) if CFG.USE_LOCAL_COMPANIES or not CFG.USE_MONGO else "None"}
          FUZZY_THRESHOLD = {CFG.FUZZY_THRESHOLD}
          DB_NAME = {CFG.DB_NAME}
          MAX_WORKERS = {CFG.MAX_WORKERS}
          TIMEOUT = {CFG.TIMEOUT}
          CREATE_CHANGES = {CFG.CREATE_CHANGES}
          USE_POLICY_CLASSIFIER = {CFG.USE_POLICY_CLASSIFIER}
          {"POLICY_CLASSIFIER_THRESHOLD = " + str(CFG.POLICY_CLASSIFIER_THRESHOLD) if CFG.USE_POLICY_CLASSIFIER else "None"}
          USE_DIFF_TRIMMER = {CFG.USE_DIFF_TRIMMER}
          {"DIFF_TRIMMER_THRESHOLD = " + str(CFG.DIFF_TRIMMER_THRESHOLD) if CFG.USE_DIFF_TRIMMER else "None"}
          {"Topic Patterns are set in the config file" if CFG.TOPIC_PATTERNS != {} else "Topic Patterns are not set in the config file"}
          {"Priority Dictionary is set in the config file: " + str(dict(Counter(CFG.PRIORITY_DICT.values()))) + " (priority: company count)" if CFG.PRIORITY_DICT != {} else "Priority Dictionary is not set in the config file"}
          
          """
    logger.info(options_string)
