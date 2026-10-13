# Transparency Archiver - Online Document Capture Tool
# This file contains a collection of scripts that are reused across the capture tool modules
# It also contains scripts that manage data between the capture tool and the database collections or document store

# Imports
from pymongo import MongoClient
from pymongo.server_api import ServerApi
import bson
from bson.objectid import ObjectId
from pathlib import Path
from charset_normalizer import from_bytes
import os
import shutil
import json
import time
import random
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import urllib.request

# Local Imports
from source.globals import CFG, logger, init_globals


def initialize_sync():
    """
    Initializes the synchronization process
    Retrieves arguments from the .env file, config file, and command line and sets up directories.
    """
    # Retrieve args, env vars, and config settings
    init_globals()
    # Create necessary directories
    # Set up directories


def join_path(path, *paths) -> Path:
    return Path(os.path.normpath(os.path.join(path, *paths)))


def normalize_seed_url(url: str) -> str:
    """
    Normalizes a url just enough to match a crawled page's recorded url back
    to the doc_urls seed it came from (fragment and trailing slash dropped,
    since the crawler may record either form). Query strings are kept, since
    they can select genuinely different content.
    """
    return url.split("#", 1)[0].rstrip("/")


def mongo_connect():
    """
    Connects to a MongoDB database.

    Returns:
        MongoClient: The MongoDB client object
    """
    # Initialize a MongoDB client
    if not CFG.USE_MONGO:
        logger.error("Cannot connect to MongoDB while USE_MONGO is False")
        raise Exception
    client = MongoClient(
        CFG.MONGODB_URL, server_api=ServerApi("1", strict=True, deprecation_errors=True)
    )
    logger.info("Successfully connected to MongoDB")
    return client


def get_all_companies() -> list:
    """
    Fetches all companies from MongoDB or specified JSON or BSON file, depending on USE_MONGO and USE_LOCAL_COMPANIES flag.
    """
    if CFG.USE_MONGO and not CFG.USE_LOCAL_COMPANIES:
        client = None
        try:
            client = mongo_connect()
            collection = client[CFG.DB_NAME]["companies"]
            logger.info("Fetching all companies from MongoDB")

            # Fetch the companies from the collection
            result = collection.find().sort({"id": -1})
            return list(result)
        except Exception as e:
            raise Exception(f"Failed to fetch companies from MongoDB: {e}")
        finally:
            # Close the client connection, if one was ever established
            if client is not None:
                client.close()
    else:
        return pull_db(CFG.COMPANIES_JSON, False)


def get_validated_companies() -> list:
    """
    Fetches and Validates a list of companies from the specified source
    This also manages company priority declared in the config file

    Returns:
      list: A list of dictionaries containing validated company data.
    """
    pData = get_all_companies()

    # Valid companies must have a _id, id, name, and doc_urls
    # Log any invalid companies
    for p in pData:
        if "_id" not in p or "id" not in p or "name" not in p or "doc_urls" not in p:
            logger.warning(f"Invalid company structure, skipping: {p}")
    # Remove any invalid companies
    pData = [
        p for p in pData if "_id" in p and "id" in p and "name" in p and "doc_urls" in p
    ]

    # Check if there is any priority filtering that needs to be done
    # First, make sure we aren't using a local companies input
    if CFG.USE_LOCAL_COMPANIES:
        return pData
    # Then, check if there is a priority list
    logger.info("Applying priority filtering, see config file for details")
    if CFG.PRIORITY_DICT != {}:
        # Then exclude any companies with a priority of 0
        pExclude = [
            p
            for p in pData
            if CFG.PRIORITY_DICT.get(str(p["_id"])) is not None
            and CFG.PRIORITY_DICT[str(p["_id"])] == 0
        ]
        pData = [p for p in pData if p not in pExclude]
        # If SYNC_PRIORITY is 1, we're all set, otherwise run one more filter
        if CFG.SYNC_PRIORITY > 1:
            # Then exclude any companies with a priority less than SYNC_PRIORITY
            pData = [
                p
                for p in pData
                if CFG.PRIORITY_DICT.get(str(p["_id"]))
                and CFG.PRIORITY_DICT[str(p["_id"])] >= CFG.SYNC_PRIORITY
            ]

    return pData


def format_data(companyData: list) -> list:
    """
    Formats the company data into a format that can be used by the crawler.
    Additionally checks to see if any documents were already crawled based on the recovery file

    Args:
      companyData (list): A list of dictionaries containing company data in the format of the MongoDB collection.

    Returns:
      list: A list of dictionaries containing company data formatted for the crawler.
    """
    listData = []

    # Iterate through the company data and make a new object for each document found
    # doc_urls entries are keyed by display name (e.g. "Privacy Policy") and hold
    # {"name": <display name>, "urls": [<url>, ...]} -- a doc type can list more
    # than one url (a document split across pages), so all of them are crawled
    # together as a single unit and merged into one captured document.
    for cObj in companyData:
        if cObj.get("doc_urls"):
            for docType, docEntry in cObj["doc_urls"].items():
                # docEntry is expected to be {"name": ..., "urls": [...]}, but
                # get_validated_companies only checks that "doc_urls" exists on
                # the company, not the shape of each entry -- a malformed or
                # still-old-schema (bare string) entry shouldn't be able to
                # crash format_data() for every OTHER company in the same run.
                urls = docEntry.get("urls") if isinstance(docEntry, dict) else None
                if not urls:
                    if docEntry:
                        logger.warning(
                            f"Skipping malformed doc_urls entry for {cObj.get('name', cObj.get('_id'))} "
                            f"({docType}): expected {{'urls': [...]}}, got {docEntry!r}"
                        )
                    continue
                listData.append(
                    {
                        "urls": urls,
                        # Typecast the _id to a string, as sometimes it's an ObjectId
                        "objId": str(cObj["_id"]),
                        "name": cObj.get("name", "Unknown"),
                        "pType": docType.replace(" ", "_").lower(),
                    }
                )

    iCData = {}
    # Iterate through the url groups to find any duplicates,
    # Then collate the requests to prevent duplicates while preserving relevant information
    logger.info(f"Excluding Recovery Data from this run: {CFG.RECOVERY_DATA}")
    for lObj in listData:
        if any(url in CFG.RECOVERY_DATA for url in lObj["urls"]):
            # This document was already captured in a previous attempted run that did not complete
            logger.info(
                f"Document {lObj['urls']} has already been captured, skipping..."
            )
            continue
        urlKey = tuple(lObj["urls"])
        if urlKey in iCData:
            # Found a duplicate url group, so let's add the company information to the same request
            logger.info(
                f"Duplicate document urls {lObj['urls']} found for {lObj['objId']}, collating requests..."
            )
            iCData[urlKey].append(lObj)
        else:
            iCData[urlKey] = [lObj]

    # Reconstruct the data into a list of (urls, entries) tuples
    formattedListData = [(list(urlKey), entries) for urlKey, entries in iCData.items()]
    # Shuffle the list of URLs to distribute them across the workers
    # Prevent any single host from getting too many simultaneous requests at once
    random.shuffle(formattedListData)

    logger.info("Data formatted for crawl")
    logger.info(f"Example data: {formattedListData[:5]}")

    return formattedListData


def upload_to_GCP(fileNames: list, googleBucket):
    """
    Uploads files from a directory to a Google Cloud Storage bucket.
    """
    if CFG.IN_CLOUD or not CFG.USE_GCP:
        logger.error(
            "Cannot upload to GCP in cloud environment or while USE_GCP is False"
        )
        raise Exception
    from google.cloud.storage import transfer_manager  # type: ignore

    # Ensure the file names are in the correct format
    try:
        # files_names should ignore the DOCS_DIR as that is not part of the GCP file path
        gcsFileNames = [
            str("/".join(p.parts[len(CFG.DOCS_DIR.parts) :])) for p in fileNames
        ]
        logger.info(
            f"Uploading {len(gcsFileNames)} files to GCP from {CFG.DOCS_DIR}..."
        )
        logger.info(f"Preview: {gcsFileNames[:1]}...")

        results = transfer_manager.upload_many_from_filenames(
            googleBucket,
            gcsFileNames,
            source_directory=str(CFG.DOCS_DIR),
            max_workers=4,
        )

        for name, result in zip(gcsFileNames, results):
            # The results list is either `None` or an exception for each blob in the input list, in order.

            if isinstance(result, Exception):
                logger.error(f"Failed to upload {name} due to exception: {result}")
            else:
                logger.info(f"Uploaded {name} to {CFG.GCP_BUCKET}.")
    except Exception as e:
        logger.error(f"An error occurred while uploading to GCP: {e}")
        raise e


def update_Mongo(results: list, collection):
    """
    Uploads a list of documents to a MongoDB collection.
    """
    if not CFG.USE_MONGO:
        logger.error("Cannot connect to MongoDB while USE_MONGO is False")
        raise Exception
    try:
        for result in results:
            try:
                collection.insert_one(result)
                formats = list(result.get("formats", {}).keys())
                logger.info(
                    f"Successfully uploaded {result['path']} (formats: {formats}) to MongoDB"
                )
            except Exception as e:
                logger.error(f"Failed to upload document to MongoDB: {e}")
    except Exception as e:
        logger.error(f"An error occurred while updating MongoDB: {e}")
        raise e


def _read_local_file_bytes(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def get_gcs_bytes(gcp_path, bucket, timeout: float = 120) -> bytes:
    """
    Returns the bytes of a file in a Google Cloud Storage bucket or from a local directory if USE_GCP is False

    Raises:
      TimeoutError: If the read doesn't complete within `timeout` seconds. Reads against the
        GCS FUSE mount (used when IN_CLOUD) are plain filesystem calls with no built-in timeout,
        so unlike the direct GCS API call below, they can hang indefinitely on a stalled mount.
        The read is run in a background thread so a stuck call can be abandoned on timeout
        instead of blocking the whole pipeline.
    """
    logger.info(f"Downloading {gcp_path} from GCS...")
    if CFG.IN_CLOUD or not CFG.USE_GCP:
        local_path = join_path(CFG.CAPTURES_DIR, gcp_path)
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_read_local_file_bytes, local_path)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            raise TimeoutError(
                f"Reading {local_path} from the GCS FUSE mount timed out after {timeout}s"
            )
        finally:
            executor.shutdown(wait=False)
    else:
        blob = bucket.blob(str(gcp_path))
        return blob.download_as_bytes(timeout=timeout)


def decode_bytes(bytes) -> str:
    """
    Decodes the bytes of a file.
    """
    text = from_bytes(bytes).best()
    if text is None:
        return str(bytes.decode("utf-8", errors="ignore"))
    else:
        return str(text)


class DocumentCache:
    """
    Simple cache for decoded document text fetched from GCS.
    Keyed by document path, evicts the least recently used entry once capacity is exceeded.
    """

    def __init__(self, capacity: int = 10):
        self.capacity = capacity
        self._cache: OrderedDict[str, str] = OrderedDict()

    def get(self, path: str) -> str | None:
        if path not in self._cache:
            return None
        # Move to end to mark as most recently used
        self._cache.move_to_end(path)
        return self._cache[path]

    def put(self, path: str, text: str) -> None:
        if path in self._cache:
            self._cache.move_to_end(path)
        else:
            if len(self._cache) >= self.capacity:
                self._cache.popitem(last=False)  # Evict LRU (front)
        self._cache[path] = text

    def __len__(self) -> int:
        return len(self._cache)


# Guards the local-mode (CFG.USE_MONGO=False) bson file append below.
# source/changes.py's create_changes_json now processes (company_id, type)
# groups concurrently via a ThreadPoolExecutor, and each group calls save_db
# independently -- without this lock, two threads' "open(..., 'ab')" +
# write() calls could interleave and corrupt the shared local database file.
_save_db_lock = threading.Lock()


def save_db(db: list[dict], dbPath: Path):
    """
    Saves a dictionary object into a local bson database file with MongoDB compatible ObjectIds

    Args:
      db (list[dict]): A list of dictionaries that represents a database collection to be appended to the database
      dbPath (Path): The bson file location to save the local database

    """
    logger.info(f"Writing new documents to {dbPath}")
    with _save_db_lock:
        with open(dbPath, "ab") as f:
            for dbItem in db:
                # Make sure all items have an ObjectId
                if dbItem.get("_id") is None:
                    new_oid = ObjectId()
                    dbItem["_id"] = new_oid
                # Save each item to the bson file
                f.write(bson.encode(dbItem))
    logger.info(f"Completed write to {dbPath}")


def pull_db(dbPath: Path, createNew: bool = True) -> list[dict]:
    """
    Load a local bson or json database from the file

    Args:
      dbPath (Path): A path to the local database file
      createNew (bool): Optional, defaults to True, when set to True will return an empty list if no file is found

    Returns:
      list: A list containing the database content
    """
    db_type = dbPath.suffix
    try:
        logger.info(f"Fetching companies from {dbPath}")
        if db_type == ".json":
            with open(dbPath, "r") as f:
                db = json.load(f)
        elif db_type == ".bson":
            with open(dbPath, "rb") as f:
                db = bson.decode_all(f.read())
        else:
            logger.error(f"Invalid file type {dbPath}. File must be .json or .bson")
            raise TypeError
        if isinstance(db, list):
            logger.info(f"Successfully fetched companies from {dbPath} as a list")
        else:
            logger.info(
                f"Successfully fetched companies from {dbPath}, attempting to convert structure to a list"
            )
            return list(db.values())
        return db
    except FileNotFoundError as e:
        if createNew:
            logger.info(
                f"No existing database found at {dbPath}, creating a new empty internal database object"
            )
            return []
        else:
            logger.error(f"Unable to find required database at {dbPath}: {e}")
            raise e
    except Exception as e:
        logger.error(
            f"There was an error retrieving the local database at {dbPath}: {e}"
        )
        raise e


def get_nested(doc: dict, dotted_key: str):
    """
    Resolves a dot-notation key (e.g. "formats.txt") against a nested dict,
    mirroring how MongoDB resolves dotted field paths in queries.
    """
    value = doc
    for part in dotted_key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def matches_query(doc: dict, query: dict) -> bool:
    """
    Checks whether a single document matches a Pymongo-style query dict.
    Supports dot-notation keys and the "$exists" operator, since that's all
    this capture process currently needs from a hand-rolled query matcher.
    """
    for key, expected in query.items():
        actual = get_nested(doc, key)
        if isinstance(expected, dict) and "$exists" in expected:
            if (actual is not None) != bool(expected["$exists"]):
                return False
        elif actual != expected:
            return False
    return True


def local_find(
    db: list[dict], query: dict, sort: list[tuple] | None = None
) -> list[dict]:
    """
    Perform Pymongo find operation on a Python list[dict] in memory
    This is only feature complete to the extent needed by this capture process

    Args:
      db (list[dict]): A python list representing the data collection
      query (dict): A dictionary of key value pairs that must be matched by the returned items
      sort (list[tuple]): Optional, defaults to None, sorts the matched items before returning

    Returns:
      list[dict]: Every item matching the query, sorted if requested
    """
    # Perform query matching
    results = [dbItem for dbItem in db if matches_query(dbItem, query)]

    # Apply all sort operations using consistent priority to pymongo find
    if sort:
        for key, direction in reversed(sort):
            results.sort(key=lambda a: a[key], reverse=(direction == -1))

    return results


def local_find_one(
    db: list[dict], query: dict, sort: list[tuple] | None = None
) -> dict | None:
    """
    Perform Pymongo find_one operation on a Python list[dict] in memory
    This is only feature complete to the extent needed by this capture process

    Args:
      db (list[dict]): A python list representing the data collection
      query (dict): A dictionary of key value pairs that must be matched by the returned item
      sort (list[tuple]): Optional, defaults to None, sorts valid results before returning the first item

    Returns:
      dict | None: The first item after filtering with the query and sorting, or None if no items matched the query
    """
    results = local_find(db, query, sort=sort)
    return results[0] if results else None


def delete_temp_gcs(bucket_name: str, prefix: str):
    """
    Deletes all objects in a Google Cloud Storage bucket with a specific prefix.
    Uses API calls instead of the GCSFuse for performance

    Args:
      bucket_name (str): The name of the bucket to delete from.
      prefix (str): The prefix of the objects to delete.
    """
    if not CFG.IN_CLOUD:
        logger.error(
            "delete_temp_gcs is a cloud-only feature, cannot run while IN_CLOUD is False"
        )
        logger.error("No files were deleted")
        return
    from google.cloud.storage import Client

    storage_client = Client()
    bucket = storage_client.get_bucket(bucket_name)
    blobs = list(bucket.list_blobs(prefix=prefix))
    if blobs:
        bucket.delete_blobs(blobs)  # batched deletion


def notify_slack(message: str):
    """
    Sends a message to the configured Slack channel via an incoming webhook.
    No-ops when USE_SLACK is False, DRY_RUN is True, or SLACK_WEBHOOK_URL is unset.
    """
    if CFG.DRY_RUN or not CFG.USE_SLACK or not CFG.SLACK_WEBHOOK_URL:
        return
    try:
        payload = json.dumps({"text": message}).encode("utf-8")
        req = urllib.request.Request(
            CFG.SLACK_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    f"Slack notification returned unexpected status {resp.status}"
                )
    except Exception as e:
        logger.warning(f"Slack notification failed: {e}")


def cleanup():
    """
    Deletes temporary files and directories used during the runtime.
    """
    # Wait a few seconds before deleting, in case there are still processes running
    time.sleep(10)

    # Delete the recovery file in the cleanup step, as all processing is complete
    try:
        os.remove(CFG.LOG_DIR / "recovery_log.txt")
        logger.info(f"Deleted {CFG.LOG_DIR / 'recovery_log.txt'}")
    except FileNotFoundError:
        logger.error(
            f"Failed to delete {CFG.LOG_DIR / 'recovery_log.txt'}, file not found"
        )
    except PermissionError:
        logger.error(
            f"Failed to delete {CFG.LOG_DIR / 'recovery_log.txt'}, permission denied"
        )
    except Exception as e:
        logger.error(
            f"An error occurred while deleting {CFG.LOG_DIR / 'recovery_log.txt'}: {e}"
        )

    # Recursively delete the temporary directory
    if CFG.IN_CLOUD:
        # Convert the TEMP_DIR to an API safe path, removing the mount point and converting to a str
        delete_temp_gcs(CFG.GCP_BUCKET, "/".join(CFG.TEMP_DIR.parts[2:]))
    else:
        shutil.rmtree(CFG.TEMP_DIR, ignore_errors=True)
    logger.info(f"Deleted {CFG.TEMP_DIR}")
