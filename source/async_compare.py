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
import asyncio
from pathlib import Path
from typing import Optional

# Local Imports
from source.globals import CFG, logger
from source.utils import get_gcs_bytes, decode_bytes


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
    ratio = fuzz.ratio(text_normalize(oldText), text_normalize(newText))
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

        fileList = wacz.namelist()
        warcFiles = [
            file
            for file in fileList
            if file.endswith(".warc") or file.endswith(".warc.gz")
        ]

        htmlFiles = [file for file in fileList if file.endswith(".html")]

        for warcFile in warcFiles:
            try:
                content = wacz.read(warcFile)
                if warcFile.endswith(".warc.gz"):
                    with gzip.open(io.BytesIO(content), "rb") as gzFile:
                        content = gzFile.read()
                text = decode_bytes(content)
                if text:
                    allText.append(text)
            except Exception as e:
                logger.error(f"Error reading WARC file: {e}")
                continue

        for htmlFile in htmlFiles:
            try:
                content = wacz.read(htmlFile)
                text = read_text_from_html(content)
                if text:
                    allText.append(text)
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


def compare_warc_files(gcsBytes: bytes, localPath: Path, gcpPath: str) -> bool:
    """
    Compares two WARC files and returns True if they have changed, False otherwise

    Args:
        gcsBytes: The bytes of the GCS file to compare
        localPath: The local path of the WARC file to compare
        gcpPath: The GCP path of the WARC file to compare

    Returns:
        bool: True if the file has changed, False otherwise
    """
    # Compare WARC files - support both wacz and warc.gz
    with open(localPath, "rb") as f:
        localBytes = f.read()

    if str(localPath).endswith(".warc.gz"):
        localMode = "warc.gz"
    else:
        localMode = "wacz"

    localText = read_wacz_file(localBytes, localMode)
    if str(gcpPath).endswith(".warc.gz"):
        gcpMode = "warc.gz"
    else:
        gcpMode = "wacz"
    gcsText = read_wacz_file(gcsBytes, gcpMode)

    # Perform fuzzy matching on text with a ratio of at least 97%
    return has_content_changed(localText, gcsText)


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


def extract_text_from_warc_json_bytes(rawBytes: bytes, originalUrl: str = "") -> str:
    """
    Extracts text from a warc.json file's raw bytes. warc.json's "records"
    list has one entry per unique page visited during the crawl, so this
    prefers the record whose urls include originalUrl (the actual target
    page) over blindly concatenating every page the crawler happened to visit.

    Args:
      rawBytes (bytes): The raw bytes of a warc.json file.
      originalUrl (str): The document's own source URL, used to select the
        matching record. If omitted or not found, every record is joined.

    Returns:
      str: The extracted text.
    """
    data = json.loads(rawBytes)
    records = data.get("records", [])
    for record in records:
        if originalUrl and originalUrl in record.get("urls", []):
            return record.get("content", "")
    return "\n\n".join(record.get("content", "") for record in records)


def reconstruct_text_from_format(
    rawBytes: bytes, format: str, extension: str, originalUrl: str = ""
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
      originalUrl (str): The document's own source URL, used to disambiguate
        warc.json records.

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


async def compare_files_async(
    localPath: Path, gcpPath: str, format: str, bucket, timeout: float = 300
) -> bool:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None, _compare_files_sync, localPath, gcpPath, format, bucket
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Comparison timed out after {timeout}s for {localPath} ({format})"
        )
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

    if format == "txt":
        # Only the txt format runs the classifier -- it was trained on text
        # captured directly by the live pipeline, and running it against
        # text extracted here from html/pdf/warc.json purely for quality
        # checking would feed it a noisier distribution than it was trained on.
        return passes_policy_document_check(text)

    return True


async def validate_file_async(
    localPath: Path, format: str, timeout: float = 300
) -> bool:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _validate_file_sync, localPath, format),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Validation timed out after {timeout}s for {localPath} ({format})"
        )
        return False


def compare_files(
    localPath: Path, gcpPath: str, format: str, bucket, timeout: float = 300
) -> bool:
    """
    Wrapper function for compare_files_async, allows for setting a timeout in case the file processing pipeline hangs

    Args:
      localPath (str): Path to the local file
      gcpPath (str): Path to the GCP file
      format (str): Format of the file
      bucket (str): Name of the GCP bucket (None if running in Google Cloud)
      timeout (float): Timeout in seconds, defaults to 300 (5 minutes)
    """
    return asyncio.run(compare_files_async(localPath, gcpPath, format, bucket, timeout))


def validate_file(localPath, format, timeout=300.0):
    """
    Wrapper function for validate_file_async, allows for setting a timeout in case the file processing pipeline hangs

    Args:
      localPath (str): Path to the local file
      format (str): Format of the file
      timeout (float): Timeout in seconds, defaults to 300 (5 minutes)
    """
    return asyncio.run(validate_file_async(localPath, format, timeout))
