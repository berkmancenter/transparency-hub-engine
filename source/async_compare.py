# Transparency Archiver - Online Document Capture Tool
# This script is used to compare the content of new and old documents
# It verifies that a document has changed, and if so, uploads it to GCS
# It also performs some basic parsing to ensure the content meets a minimum data quality standard
# And attempts to ensure content is presented in English

# Imports
import zipfile
import gzip
import io
from bs4 import BeautifulSoup
from bs4.element import NavigableString
import PyPDF2
from rapidfuzz import fuzz
import re
import json
import langdetect
import concurrent.futures
from pathlib import Path
from typing import Optional

# Local Imports
from source.globals import CFG, logger
from source.utils import get_gcs_bytes, decode_bytes, normalize_seed_url


# Caps the input handed to rapidfuzz's C++ implementation. rapidfuzz raises a raw
# C++ std::bad_alloc (not Python's catchable MemoryError) if an internal allocation
# fails, which crashes the whole process via std::terminate -- no try/except here can
# stop that. A malformed or duplicated reconstruction (see process.py
# reconstruct_missing_txt) could otherwise hand it a pathologically large string; real
# policy documents are far smaller than this cap, so truncating doesn't affect them.
MAX_FUZZY_COMPARE_CHARS = 500_000


def has_content_changed(
    oldText: str, newText: str, threshold: Optional[int] = None
) -> bool:
    """
    Identifies if the content of two documents has changed
    Uses fuzzy matching to determine if the content has changed while ignoring small changes that might've occurred from website formatting differences

    Args:
        oldText (str): The text of the old document
        newText (str): The text of the new document
        threshold (int, optional): The threshold for fuzzy matching. Defaults to the live CFG.FUZZY_THRESHOLD value.

    Returns:
        bool: True if the content has changed beyond the threshold, False otherwise
    """
    if threshold is None:
        threshold = CFG.FUZZY_THRESHOLD

    def text_normalize(text: str) -> str:
        """
        Removes special characters and long whitespace and converts to lowercase for fuzzy matching

        Args:
          text (str): The text to normalize

        Returns:
          str: The normalized text
        """
        text = re.sub(r"[^\w+]", " ", text)
        return text.lower().strip()

    logger.debug(f"Checking if content has changed with threshold {threshold}")
    logger.debug(f"Old text: {oldText[:1000]}")
    logger.debug(f"New text: {newText[:1000]}")

    # Perform fuzzy matching on text with a ratio of at least 97%
    ratio = fuzz.ratio(
        text_normalize(oldText[:MAX_FUZZY_COMPARE_CHARS]),
        text_normalize(newText[:MAX_FUZZY_COMPARE_CHARS]),
    )
    return ratio < threshold


def read_text_from_html(htmlObj: bytes) -> str:
    """
    Converts an html object to a readable text string

    Args:
      htmlObj (bytes): The html object to convert as bytes

    Returns:
      str: The converted text
    """
    # Convert the html to soup using the lxml parser for speed
    soup = BeautifulSoup(htmlObj, features="lxml")

    # Convert the soup to text using html2text library
    remove_tags = ["style", "img", "script"]
    for rTag in remove_tags:
        for tag in soup.find_all(rTag):
            tag.decompose()

    for tag in soup.find_all("a"):
        tag.replace_with(NavigableString(tag.get_text()))

    return soup.get_text(separator=" ", strip=True)


# Caps the total text accumulated while unzipping/decompressing a WACZ archive.
# This is the last-resort format in RECONSTRUCTION_FORMAT_PRIORITY (raw WARC/WACZ is
# the heaviest and messiest source), and unlike the other formats it has no inherent
# size limit -- a page with a lot of captured network traffic (large data tables, many
# embedded resources) can produce a WACZ whose fully decompressed, joined text is many
# times larger than the page itself. That's cheap to allocate in Python but can trip an
# uncaught C++ std::bad_alloc in a downstream native call (rapidfuzz, lxml) that isn't
# translated into a catchable MemoryError, crashing the whole process. Stopping
# accumulation once this cap is hit keeps the feature working for the WACZ sizes actually
# produced by a single crawled page, while bounding the pathological case.
MAX_WACZ_EXTRACT_CHARS = 2_000_000

# Guards against a single pathologically large entry within the WACZ (e.g. a page whose
# WARC response body is much bigger than typical page text) blowing past
# MAX_WACZ_EXTRACT_CHARS in one shot -- the cumulative cap above only stops *further*
# entries once crossed, so a single oversized one needs its own check before it's read
# and decompressed at all. zipfile's central-directory metadata reports each entry's
# uncompressed size without having to read/decompress it first.
MAX_WACZ_ENTRY_BYTES = 20_000_000


def read_wacz_file(data: bytes, mode: str = "wacz") -> str:
    """
    A wrapper for extracting text from a WARC file bundle

    Args:
      data: The WARC file bundle to extract text from

    Returns:
      str: The extracted text joined together
    """

    def extract_text_from_wacz(wacz: zipfile.ZipFile) -> list:
        """
        Attempts to extract text from a WARC file bundle

        Args:
          wacz: The WARC file to extract text from

        Returns:
          list: A list of all the extracted text
        """
        allText = []
        totalChars = 0

        fileList = wacz.namelist()
        warcFiles = [
            file
            for file in fileList
            if file.endswith(".warc") or file.endswith(".warc.gz")
        ]

        htmlFiles = [file for file in fileList if file.endswith(".html")]

        for warcFile in warcFiles:
            if totalChars >= MAX_WACZ_EXTRACT_CHARS:
                logger.warning(
                    f"WACZ text extraction hit the {MAX_WACZ_EXTRACT_CHARS}-char cap, "
                    f"skipping remaining WARC entries"
                )
                break
            if wacz.getinfo(warcFile).file_size > MAX_WACZ_ENTRY_BYTES:
                logger.warning(
                    f"Skipping oversized WARC entry {warcFile} "
                    f"({wacz.getinfo(warcFile).file_size} bytes)"
                )
                continue
            try:
                content = wacz.read(warcFile)
                if warcFile.endswith(".warc.gz"):
                    with gzip.open(io.BytesIO(content), "rb") as gzFile:
                        content = gzFile.read()
                text = decode_bytes(content)
                if text:
                    allText.append(text)
                    totalChars += len(text)
            except Exception as e:
                logger.error(f"Error reading WARC file: {e}")
                continue

        for htmlFile in htmlFiles:
            if totalChars >= MAX_WACZ_EXTRACT_CHARS:
                logger.warning(
                    f"WACZ text extraction hit the {MAX_WACZ_EXTRACT_CHARS}-char cap, "
                    f"skipping remaining HTML entries"
                )
                break
            if wacz.getinfo(htmlFile).file_size > MAX_WACZ_ENTRY_BYTES:
                logger.warning(
                    f"Skipping oversized HTML entry {htmlFile} "
                    f"({wacz.getinfo(htmlFile).file_size} bytes)"
                )
                continue
            try:
                content = wacz.read(htmlFile)
                text = read_text_from_html(content)
                if text:
                    allText.append(text)
                    totalChars += len(text)
            except Exception as e:
                logger.error(f"Error reading HTML file: {e}")
                continue

        return allText

    allText = []

    try:
        # warc.gz files need to be unzipped differently
        if mode == "warc.gz":
            with gzip.open(io.BytesIO(data), "rb") as gzFile:
                with zipfile.ZipFile(gzFile, "r") as wacz:
                    allText = extract_text_from_wacz(wacz)
        elif mode == "wacz":
            with zipfile.ZipFile(io.BytesIO(data), "r") as wacz:
                allText = extract_text_from_wacz(wacz)

        return "\n\n".join(allText)
    except Exception as e:
        logger.error(f"Error reading WARC file: {e}")
        return ""


def compare_html_files(gcsBytes: bytes, localPath: Path) -> bool:
    # Compare HTML files
    with open(localPath, "rb") as f:
        htmlFile = f.read()

    # Perform fuzzy matching on text with a ratio of at least 97%
    return has_content_changed(
        read_text_from_html(htmlFile), read_text_from_html(gcsBytes)
    )


def compare_txt_files(gcsBytes: bytes, localPath: Path) -> bool:
    """
    Compares two TXT files and returns True if they have changed, False otherwise

    Args:
      gcsBytes: The bytes of the GCS file to compare
      localPath: The local path of the TXT file to compare

    Returns:
      bool: True if the file has changed, False otherwise
    """
    # Open the local TXT into a bytes object
    with open(localPath, "rb") as f:
        localBytes = f.read()

    # Decode the text using charset_normalizer to attempt to get the correct encoding
    localText = decode_bytes(localBytes)
    gcsText = decode_bytes(gcsBytes)

    # Perform fuzzy matching on text with a ratio of at least 97%
    return has_content_changed(localText, gcsText)


def compare_pdf_files(gcsBytes: bytes, localPath: Path) -> bool:
    """
    Compares two PDF files and returns True if they have changed, False otherwise

    Args:
      gcsBytes: The bytes of the GCS file to compare
      localPath: The local path of the PDF file to compare

    Returns:
      bool: True if the file has changed, False otherwise
    """

    # Open the local PDF and extract the text
    with open(localPath, "rb") as f:
        localPdf = PyPDF2.PdfReader(f)
        localText = ""
        for page in localPdf.pages:
            localText += page.extract_text()

    # Open the GCS PDF and extract the text
    gcsPdf = PyPDF2.PdfReader(io.BytesIO(gcsBytes))
    gcsText = ""
    for page in gcsPdf.pages:
        gcsText += page.extract_text()

    # Perform fuzzy matching on text with a ratio of at least 97%
    return has_content_changed(localText, gcsText)


def compare_json_files(gcsBytes: bytes, localPath: Path) -> bool:
    """
    Compares two JSON files and returns True if they have changed, False otherwise

    Args:
      gcsBytes: The bytes of the GCS file to compare
      localPath: The local path of the JSON file to compare

    Returns:
      bool: True if the file has changed, False otherwise
    """
    with open(localPath, "rb") as f:
        localJson = json.load(f)

    gcsJson = json.loads(gcsBytes)

    def extract_json_text(warcJson):
        """
        Extracts the text from a WARC JSON object

        Args:
          warcJson: The WARC JSON object to extract the text from

        Returns:
          str: The text extracted from the WARC JSON object
        """
        records = warcJson.get("records", [])
        allText = []
        for record in records:
            if "content" in record:
                allText.append(record["content"])
        return "\n".join(allText)

    # Perform fuzzy matching on text with a ratio of at least 97%
    return has_content_changed(extract_json_text(localJson), extract_json_text(gcsJson))


def passes_policy_document_check(text: str) -> bool:
    """
    Runs the trained policy-document classifier (source/policy_classifier.py)
    against text destined to become a .txt capture (whether captured directly
    or reconstructed from another format), rejecting captures the model is
    confident are NOT an actual policy document (homepage, cookie wall, 404
    page, login wall, ...).

    Fails open (returns True) if CFG.USE_POLICY_CLASSIFIER is False or if the
    classifier can't be loaded/run, so a missing or broken model file
    degrades to pre-classifier behavior instead of blocking every capture.

    Args:
      text (str): The candidate .txt document text to classify.

    Returns:
      bool: True if the text should be treated as a valid policy document.
    """
    if not CFG.USE_POLICY_CLASSIFIER:
        return True
    try:
        from source.policy_classifier import is_policy_document

        return is_policy_document(text, threshold=CFG.POLICY_CLASSIFIER_THRESHOLD)
    except Exception as e:
        logger.warning(
            f"Policy-document classifier check failed, allowing capture through: {e}"
        )
        return True


RECONSTRUCTION_FORMAT_PRIORITY = ["html", "pdf", "warc.json", "warc"]


def extract_text_from_pdf_bytes(rawBytes: bytes) -> str:
    """
    Extracts text from raw PDF bytes.

    Args:
      rawBytes (bytes): The raw bytes of a PDF file.

    Returns:
      str: The extracted text.
    """
    reader = PyPDF2.PdfReader(io.BytesIO(rawBytes))
    return "".join(page.extract_text() for page in reader.pages)


def extract_text_from_warc_json_bytes(
    rawBytes: bytes, originalUrl: str | list[str] = ""
) -> str:
    """
    Extracts text from a warc.json file's raw bytes. warc.json's "records"
    list has one entry per unique page visited during the crawl, so this
    prefers the records whose urls include one of originalUrl's (the actual
    target page(s)) over blindly concatenating every page the crawler
    happened to visit. A document can be captured from more than one seed
    url (a document split across pages), in which case every matching
    record's content is joined -- mirroring how a directly-captured .txt
    merges multiple pages (see process.py ProcessCrawl.merge_txt_files).

    Args:
      rawBytes (bytes): The raw bytes of a warc.json file.
      originalUrl (str | list[str]): The document's own source url(s), used
        to select matching records. Accepts a single url (legacy captures
        stored it as a plain string) or a list. If omitted or none are
        found, every record is joined instead.

    Returns:
      str: The extracted text.
    """
    data = json.loads(rawBytes)
    records = data.get("records", [])

    targetUrls = [originalUrl] if isinstance(originalUrl, str) else list(originalUrl)
    targetUrls = [url for url in targetUrls if url]

    if targetUrls:
        # records are in the order the crawler finished pages, which varies
        # from run to run -- order the matches by seed url instead, the same
        # order merge_txt_files uses, so the text is stable across captures
        seedIndex = {}
        for i, url in enumerate(targetUrls):
            seedIndex.setdefault(normalize_seed_url(url), i)
        ranked = []
        for record in records:
            ranks = [
                seedIndex[normalize_seed_url(url)]
                for url in record.get("urls", [])
                if normalize_seed_url(url) in seedIndex
            ]
            if ranks:
                ranked.append((min(ranks), record.get("content", "")))
        if ranked:
            ranked.sort(key=lambda r: r[0])
            return "\n\n".join(content for _, content in ranked)

    return "\n\n".join(record.get("content", "") for record in records)


def reconstruct_text_from_format(
    rawBytes: bytes, format: str, extension: str, originalUrl: str | list[str] = ""
) -> str:
    """
    Extracts text from an already-captured format, used to backfill a
    missing .txt capture when the crawl itself didn't produce one directly.

    Args:
      rawBytes (bytes): The raw bytes of the already-captured format's file.
      format (str): The format key to reconstruct from ("html", "pdf",
        "warc.json", or "warc").
      extension (str): The file extension of the captured format, used to
        distinguish "warc" stored as .wacz vs .warc.gz.
      originalUrl (str | list[str]): The document's own source url(s), used
        to disambiguate warc.json records. Accepts a single url (legacy
        captures) or a list (current captures, which can have more than one
        seed url).

    Returns:
      str: The extracted text, or an empty string for an unrecognized format.
    """
    if format == "html":
        return read_text_from_html(rawBytes)
    if format == "pdf":
        return extract_text_from_pdf_bytes(rawBytes)
    if format == "warc.json":
        return extract_text_from_warc_json_bytes(rawBytes, originalUrl)
    if format == "warc":
        mode = "warc.gz" if extension.endswith("warc.gz") else "wacz"
        return read_wacz_file(rawBytes, mode)
    return ""


def data_quality_check(text: Optional[str]) -> bool:
    # If the text is None or empty, return False
    if text is None or len(text.strip()) == 0:
        return False

    words = text.split(" ")
    # If the number of words is less than 20, return False
    if len(words) < 20:
        return False

    try:
        # Use LangDetect to determine if the text is in English
        return langdetect.detect(text[:3000]) == "en"
    except langdetect.lang_detect_exception.LangDetectException:
        # If LangDetect fails, don't assume anything, just return True
        return True
    except Exception:
        # If any other error occurs, don't assume anything, just return True
        return True


def _compare_files_sync(localPath: Path, gcpPath: str, format: str, bucket) -> bool:
    if format == "warc":
        return True

    gcs_bytes = get_gcs_bytes(gcpPath, bucket)

    if format == "html":
        return compare_html_files(gcs_bytes, localPath)
    if format == "txt":
        return compare_txt_files(gcs_bytes, localPath)
    if format == "pdf":
        return compare_pdf_files(gcs_bytes, localPath)
    if format == "warc.json":
        return compare_json_files(gcs_bytes, localPath)
    logger.error(f"Unknown format pushed to compare_files: {format}")
    return False


def _validate_file_sync(localPath: Path, format: str) -> bool:
    if format == "warc":
        return True
    text = ""

    if format == "html":
        with open(localPath, "rb") as f:
            htmlFile = f.read()
        text = read_text_from_html(htmlFile)

    if format == "txt":
        with open(localPath, "rb") as f:
            localBytes = f.read()
        text = decode_bytes(localBytes)

    if format == "pdf":
        with open(localPath, "rb") as f:
            localPdf = PyPDF2.PdfReader(f)
            for page in localPdf.pages:
                text += page.extract_text()

    if format == "warc.json":
        with open(localPath, "rb") as f:
            localJson = json.load(f)

        for record in localJson["records"]:
            if "content" in record:
                text += record["content"]

    if not data_quality_check(text):
        return False

    if format in ("txt", "warc.json"):
        return passes_policy_document_check(text)

    return True


# compare_files/validate_file each run their sync counterpart in a fresh,
# single-use worker thread rather than a shared pool. An earlier version of
# this code (briefly) used one shared, bounded ThreadPoolExecutor for every
# call -- but future.result(timeout=...) only stops WAITING on a timeout, it
# can't cancel the underlying thread, so a genuinely stuck call (e.g. a hung
# GCS FUSE read inside get_gcs_bytes, source/utils.py) keeps occupying one of
# the shared pool's limited worker slots for as long as ITS OWN internal
# timeout takes to fire. Enough concurrent hangs could exhaust every shared
# slot, making every OTHER in-flight comparison/validation queue behind them
# and time out too, even though its own file was perfectly fine. A fresh
# executor per call (matching get_gcs_bytes' own pattern) keeps a stuck call
# isolated to its own abandoned thread, exactly like the old per-call asyncio
# event loop this replaced.
def _run_with_timeout(func, args: tuple, timeout: float, timeout_message: str):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(func, *args)
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        logger.warning(timeout_message)
        return False
    finally:
        executor.shutdown(wait=False)


def compare_files(
    localPath: Path, gcpPath: str, format: str, bucket, timeout: float = 300
) -> bool:
    """
    Wrapper for _compare_files_sync, allows for setting a timeout in case the file processing pipeline hangs

    Args:
      localPath (str): Path to the local file
      gcpPath (str): Path to the GCP file
      format (str): Format of the file
      bucket (str): Name of the GCP bucket (None if running in Google Cloud)
      timeout (float): Timeout in seconds, defaults to 300 (5 minutes)
    """
    return _run_with_timeout(
        _compare_files_sync,
        (localPath, gcpPath, format, bucket),
        timeout,
        f"Comparison timed out after {timeout}s for {localPath} ({format})",
    )


def validate_file(localPath, format, timeout=300.0):
    """
    Wrapper for _validate_file_sync, allows for setting a timeout in case the file processing pipeline hangs

    Args:
      localPath (str): Path to the local file
      format (str): Format of the file
      timeout (float): Timeout in seconds, defaults to 300 (5 minutes)
    """
    return _run_with_timeout(
        _validate_file_sync,
        (localPath, format),
        timeout,
        f"Validation timed out after {timeout}s for {localPath} ({format})",
    )
