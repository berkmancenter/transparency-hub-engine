# Transparency Archiver - Online Document Capture Tool
# This script creates the CHANGES.json file format used by the Comparison Tool on the webapp
# This file is designed to run within the sync, but can also be ran as a standalone script

# Imports
import gc
import json
import re
import os
from datetime import timezone

# Local Imports
from source.globals import CFG, logger
from source.utils import (
    mongo_connect,
    get_gcs_bytes,
    decode_bytes,
    DocumentCache,
    pull_db,
    local_find,
    save_db,
)

if CFG.USE_MONGO:
    from pymongo.collection import Collection


def _fetch_and_decode_with_retries(
    path: str, bucket, doc_cache: DocumentCache, retries: int = 2
) -> str | None:
    """
    Fetches and decodes a document's text via get_gcs_bytes, retrying on timeout.
    Returns None (after logging) if it still fails after all retries, so the caller
    can skip this pair instead of letting one stuck read crash the entire run.
    """
    for attempt in range(1, retries + 2):
        try:
            text = decode_bytes(get_gcs_bytes(path, bucket))
            doc_cache.put(path, text)
            return text
        except TimeoutError as e:
            logger.error(f"Attempt {attempt} failed fetching {path}: {e}")
    logger.error(f"Giving up on {path} after {retries + 1} attempts, skipping this pair")
    return None


def _fetch_html_trimmed(
    capture: dict, bucket, html_cache: DocumentCache
) -> str | None:
    """
    Fetches a capture's html format (if it has one) and trims header/footer/
    nav boilerplate off it via source/boundary_classifier.py, purely so the
    diff written to .CHANGES.json reads cleaner in the Comparison Tool.

    This is cosmetic only -- it's never used for change-detection, and a
    None return (no html format, fetch failure, or classifier failure) just
    tells the caller to fall back to the full untrimmed .txt, exactly as
    before this existed.
    """
    formats = capture.get("formats") or {}
    if "html" not in formats:
        return None

    try:
        # Extension lookup included in the try/except -- malformed or legacy
        # formats.html metadata (e.g. missing "extension") must fail open to
        # the untrimmed .txt like any other failure here, not crash the run.
        extension = formats["html"]["extension"]
        path = f"{capture['path']}.{extension}"

        cached = html_cache.get(path)
        if cached is not None:
            return cached

        from source.boundary_classifier import extract_policy_text

        html_bytes = get_gcs_bytes(path, bucket)
        trimmed = extract_policy_text(html_bytes, threshold=CFG.DIFF_TRIMMER_THRESHOLD)
        if not trimmed:
            # No blocks extracted at all -- an empty diff would be more
            # confusing than useful, so treat this the same as a failure
            # and let the caller fall back to the untrimmed .txt.
            return None
        html_cache.put(path, trimmed)
        return trimmed
    except Exception as e:
        logger.warning(f"Diff trimmer failed for {capture['path']}, using untrimmed text instead: {e}")
        return None


def get_diff_text(
    olderCapture: dict,
    newerCapture: dict,
    bucket,
    doc_cache: DocumentCache,
    html_cache: DocumentCache,
) -> tuple[str | None, str | None]:
    """
    Returns the (older, newer) text to build a .CHANGES.json diff from.

    When CFG.USE_DIFF_TRIMMER is on and both captures have an html format,
    both sides are trimmed via the same extraction pipeline (never just
    one side, which would show a diff full of extraction-style noise rather
    than real content changes). Otherwise -- or if either side's html fetch
    or trim fails -- both sides fall back to the full, untrimmed .txt.
    """
    if CFG.USE_DIFF_TRIMMER:
        olderTrimmed = _fetch_html_trimmed(olderCapture, bucket, html_cache)
        newerTrimmed = _fetch_html_trimmed(newerCapture, bucket, html_cache)
        if olderTrimmed is not None and newerTrimmed is not None:
            return olderTrimmed, newerTrimmed

    olderPath = f"{olderCapture['path']}.txt"
    newerPath = f"{newerCapture['path']}.txt"

    olderText = doc_cache.get(olderPath)
    if olderText is None:
        logger.info(f"Cache miss - fetching {olderPath}")
        olderText = _fetch_and_decode_with_retries(olderPath, bucket, doc_cache)
    else:
        logger.info(f"Cache hit - {olderPath}")

    newerText = doc_cache.get(newerPath)
    if newerText is None:
        logger.info(f"Cache miss - fetching {newerPath}")
        newerText = _fetch_and_decode_with_retries(newerPath, bucket, doc_cache)
    else:
        logger.info(f"Cache hit - {newerPath}")

    return olderText, newerText


def tokenize(text: str) -> list:
    """
    Split text into words while preserving whitespace and punctuation as separate tokens.

    Args:
      text (str): The text to split into words and whitespace tokens

    Returns:
      list: A list of the separated tokens in order
    """
    # Split on word boundaries but keep all characters
    if not text:
        return []

    tokens = re.findall(r"\w+|\s+|[^\w\s]+", text)
    return tokens


def analyze_topic_changes(addedText: str, removedText: str) -> dict:
    """
    Analyze text changes for key topics using the TOPIC_PATTERNS found in the config.

    Args:
      addedText (str): A string of all document text detected as added by the diff method
      removedText (str): A string of all document text detected as removed by the diff method

    Returns:
      dict: A dictionary that contains any topic changes on the addedText or removedText as well as the patterns used to identify these topics
    """
    results = {}

    # Iterate through the topic patterns from the config
    for topic, patterns in CFG.TOPIC_PATTERNS.items():
        # Find matches in the addedText and removedText
        addedMatch = any(p.search(addedText) for p in patterns)
        removedMatch = any(p.search(removedText) for p in patterns)

        # Preserve the match and patterns to save in the Changes file
        if addedMatch or removedMatch:
            results[topic] = [
                addedMatch,
                removedMatch,
                # Convert the regex patterns to strings for storage
                [str(pattern) for pattern in patterns],
            ]

    return results


def separate_other_chars(tokens: list) -> tuple:
    """
    Iterate through a list of tokens and split them into words and whitespace while preserving structure

    Args:
      tokens (list): A list of word tokens and whitespace or punctuation tokens

    Returns:
      tuple (list, dict): A tuple containing an ordered list of the words in the document and a map of non-word characters that should be ignored in the diff method
    """
    words = []
    otherCharsMap = {}
    currentChars = ""

    for token in tokens:
        if re.match(r"^\w+", token):
            # This is a word token - flush accumulated non-word chars, then add word
            if currentChars:
                otherCharsMap[len(words)] = currentChars
                currentChars = ""
            words.append(token)
        else:
            # This is whitespace/punctuation - accumulate it
            currentChars += token

    # Handle trailing characters
    if currentChars:
        otherCharsMap[len(words)] = currentChars

    return words, otherCharsMap


def patience_diff_tokens(tokens1: list, tokens2: list) -> list:
    """
    Perform a patience diff on the tokens, finding the optimal matching between longer sequences within the document
    This is not the most performance optimized diff, but should give the best visual results with the inconsistent input formatting

    Args:
      tokens1 (list): A list of the word tokens from the past document
      tokens2 (list): A list of the word tokens from the new document

    Returns:
      list: A list with the diff data for each token represented using booleans
    """

    def find_unique_matches(t1: list, t2: list) -> list:
        """
        Find the unique token matches between t1 and t2
        """
        count1 = {}
        count2 = {}

        # Map the lists of tokens into a dictionary to find the number of occurences
        for token in t1:
            count1[token] = count1.get(token, 0) + 1
        for token in t2:
            count2[token] = count2.get(token, 0) + 1

        # If the counts for a token in both input lists are both 1 then there's a unique match
        matches = []
        for i, token in enumerate(t1):
            if count1[token] == 1 and count2.get(token, 0) == 1:
                j = t2.index(token)
                matches.append((i, j))

        return matches

    def longest_increasing_subsequence(matches: list):
        """
        Find the longest increasing subsequence from an input of matches represented by the indices of the matching tokens
        """
        if not matches:
            return []

        n = len(matches)
        dp = [1] * n
        parent = [-1] * n

        # Iterate through the matches to find a set of continuous progressing matches based on the token indices using dp and parent
        for i in range(1, n):
            for j in range(i):
                if matches[j][1] < matches[i][1]:
                    if dp[j] + 1 > dp[i]:
                        dp[i] = dp[j] + 1
                        parent[i] = j

        # Find the longest subsequence represented by the maximum value of dp
        max_idx = dp.index(max(dp))
        lcs = []
        idx = max_idx
        # Loop until idx is -1, which means we've hit the end of the continuous match
        while idx != -1:
            lcs.append(matches[idx])
            idx = parent[idx]
        lcs.reverse()

        return lcs

    def diff_recursive(
        t1: list, t2: list, lo1: int, hi1: int, lo2: int, hi2: int
    ) -> list:
        """
        Recursively finds the next longest matching subsequence in the subset of t1 and t2 defined by the inputs as indices

        Args:
          t1 (list): A list of tokens from the past document
          t2 (list): A list of tokens from the new document
          lo1 (int): The lower index of the subset of t1 we want to diff
          hi1 (int): The upper index of the subset of t1 we want to diff
          lo2 (int): The lower index of the subset of t2 we want to diff
          hi2 (int): The upper index of the subset of t2 we want to diff

        Returns:
          list: A list of objects with the token and booleans that establish whether that token was added, removed, or neither
        """
        # Handle when t1 and t2 are different lengths
        if lo1 >= hi1 and lo2 >= hi2:
            # Hit the end of both t1 and t2
            return []
        if lo1 >= hi1:
            # Hit the end of t1, any remaining t2 was added
            return [(token, True, False) for token in t2[lo2:hi2]]
        if lo2 >= hi2:
            # Hit the end of t2, any remaining t1 was removed
            return [(token, False, True) for token in t1[lo1:hi1]]

        # Find unique matches in the relevant subset of the tokens
        sub1 = t1[lo1:hi1]
        sub2 = t2[lo2:hi2]
        matches = find_unique_matches(sub1, sub2)

        # No matches were found, all content in this subsection of t1 was removed and all content in this subsection of t2 was added
        if not matches:
            result = [(token, False, True) for token in sub1]
            result.extend([(token, True, False) for token in sub2])
            return result

        # Find the longest increasing subsequence across this subset of the diff
        # This is the important structure of the patience diff, it prioritizes the largest possible match within the subset
        lcs = longest_increasing_subsequence(matches)

        result = []
        prev1, prev2 = 0, 0

        for match1, match2 in lcs:
            # Recursively diff the sections before this match
            result.extend(
                diff_recursive(
                    t1, t2, lo1 + prev1, lo1 + match1, lo2 + prev2, lo2 + match2
                )
            )
            # Add the matching token
            result.append((t1[lo1 + match1], False, False))
            prev1 = match1 + 1
            prev2 = match2 + 1

        result.extend(diff_recursive(t1, t2, lo1 + prev1, hi1, lo2 + prev2, hi2))

        return result

    return diff_recursive(tokens1, tokens2, 0, len(tokens1), 0, len(tokens2))


def char_diff(old: str, new: str) -> list:
    """
    Simple character-level LCS diff between two short strings.
    Returns list of (char, isAdded, isRemoved) tuples.
    Used only for punctuation/whitespace segments inside changed regions.
    """
    # Build LCS table
    m, n = len(old), len(new)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if old[i - 1] == new[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # Traceback
    result = []
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and old[i - 1] == new[j - 1]:
            result.append((old[i - 1], False, False))
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            result.append((new[j - 1], True, False))
            j -= 1
        else:
            result.append((old[i - 1], False, True))
            i -= 1
    result.reverse()
    return result


def reconstruct_with_whitespace(wordDiff: list, sp1: dict, sp2: dict) -> list:
    """
    Rebuild the document with whitespace and special characters after performing the diff

    Args:
      wordDiff (list): A list of Diff data from that consists of (token: str, isAdded: bool, isRemoved: bool)
      sp1 (dict): A dictionary containing whitespace and special characters with indices from where they were in the original past document
      sp2 (dict): A dictionary containing whitespace and special characters with indices from where they were in the original new document

    Returns:
      list: A list of objects with the token and booleans that establish whether that token was added, removed, or neither
    """
    result = []
    idx1 = 0
    idx2 = 0
    # Track the status of the previous word token so whitespace/special characters
    # can inherit it. Starts as (False, False) so leading whitespace is unchanged.
    prevAdded = False
    prevRemoved = False

    for word, isAdded, isRemoved in wordDiff:
        if isRemoved:
            # Use special characters from past document
            if idx1 in sp1 and sp1[idx1]:
                result.append((sp1[idx1], prevAdded, prevRemoved))
            result.append((word, isAdded, isRemoved))
            idx1 += 1
        elif isAdded:
            # Use special characters from new document
            if idx2 in sp2 and sp2[idx2]:
                result.append((sp2[idx2], prevAdded, prevRemoved))
            result.append((word, isAdded, isRemoved))
            idx2 += 1
        else:
            # Unchanged word - prefer new special characters, fall back to old
            ws = sp2.get(idx2, sp1.get(idx1, ""))
            if ws:
                result.append((ws, prevAdded, prevRemoved))
            result.append((word, isAdded, isRemoved))
            idx1 += 1
            idx2 += 1

        # Update previous token status after appending the word
        prevAdded = isAdded
        prevRemoved = isRemoved

    # Handle any trailing whitespace, inherits the last word's status
    if idx1 in sp1:
        result.append((sp1[idx1], prevAdded, prevRemoved))
    elif idx2 in sp2:
        result.append((sp2[idx2], prevAdded, prevRemoved))

    return result


def collate_diff(diffResult: list) -> list:
    """
    Iterate through the diff data and collate sequences of added, removed, or unchanged content
    Considerably shrinks the filesize and optimizes handling of the .CHANGES.json files

    Args:
      diffResult (list): A list of the computed diff data with an entry for each individual token

    Returns:
      list: A list of the computed diff data with one entry for each sequence of added, removed, or unchanged content
    """
    # Handle an empty input
    if not diffResult:
        return []

    # Initialize with the first token of the diffResult
    collated = []
    currentText = diffResult[0][0]
    currentAdded = diffResult[0][1]
    currentRemoved = diffResult[0][2]

    # Iterate through the diffResult
    for token, isAdded, isRemoved in diffResult[1:]:
        if isAdded == currentAdded and isRemoved == currentRemoved:
            # Same flags, combine the text
            currentText += token
        else:
            # Different flags, save current and start new
            collated.append((currentText, currentAdded, currentRemoved))
            currentText = token
            currentAdded = isAdded
            currentRemoved = isRemoved

    # Don't forget the last group
    collated.append((currentText, currentAdded, currentRemoved))

    return collated


def create_patience_diff_data(oldText: str, newText: str):
    """
    Wrapper for the suite of diff functions, uses a patience diff to prioritize longer matching sequences while ignoring whitespace and special characters

    Args:
      oldText (str): A string of the past document text
      newText (str): A string of the new document text

    Returns:
      list: An ordered list of the document text with added and removed boolean flags
    """
    # Convert the strings into tokens
    tokens1 = tokenize(oldText)
    tokens2 = tokenize(newText)

    # Remove whitespace and special characters from the diff token set
    words1, sp1 = separate_other_chars(tokens1)
    words2, sp2 = separate_other_chars(tokens2)

    # Calculate the diff data on the words
    wordDiff = patience_diff_tokens(words1, words2)

    # Rebuild the document with the whitespace and special characters
    result = reconstruct_with_whitespace(wordDiff, sp1, sp2)

    # Collate the diff list for overhead optimization
    result = collate_diff(result)

    return result


# Open connection to Mongo here so that it can be reused in the loop
def create_changes_json(idSet: set | None = None):
    """
    Creates .CHANGES.json objects to be used by the Transparency Hub's Comparison Tool

    Args:
      idSet (set, optional): Set of company IDs to process. Defaults to None.
    """
    localChangesDB: list
    mongoChangesDB: Collection
    idList = list(idSet) if idSet else None

    # Connect to external dependencies
    if CFG.USE_MONGO:
        logger.info("Connecting to MongoDB...")
        mongoClient = mongo_connect()
        collection = mongoClient[CFG.DB_NAME]["documents_v2"]
        mongoChangesDB = mongoClient[CFG.DB_NAME]["changes"]
        # Mongo pipeline to structure the documents data for creating Change files
        pipeline = [
            {
                "$match": {
                    "formats.txt": {"$exists": True},
                    # Filter by the company IDs, if provided by idSet
                    **({"company_id": {"$in": idList}} if idList else {}),
                }
            },
            {"$sort": {"date_fetched": 1}},
            {
                "$group": {
                    "_id": {
                        "company_id": "$company_id",
                        "type": "$type",
                    },
                    "captures": {
                        "$push": {
                            "_id": "$_id",
                            "path": "$path",
                            "date_fetched": "$date_fetched",
                            "formats": "$formats",
                        }
                    },
                }
            },
        ]
        grouped_captures = list(collection.aggregate(pipeline))
    else:
        collection = pull_db(CFG.DOCUMENTS_BSON)
        localChangesDB = pull_db(CFG.CHANGES_BSON)

        def local_aggregate(
            documents: list[dict], id_set: set | None = None
        ) -> list[dict]:
            # $match: filter by presence of a txt format and optionally by company_id
            matched = [
                doc
                for doc in documents
                if "txt" in doc.get("formats", {})
                and (id_set is None or doc.get("company_id") in id_set)
            ]

            # $sort: sort by date_fetched ascending
            sorted_docs = sorted(matched, key=lambda d: d["date_fetched"])

            # $group: group by (company_id, type), pushing capture fields
            groups: dict[tuple, list] = {}
            for doc in sorted_docs:
                key = (doc.get("company_id"), doc.get("type"))
                groups.setdefault(key, []).append(
                    {
                        "_id": doc["_id"],
                        "path": doc.get("path"),
                        "date_fetched": doc["date_fetched"],
                        "formats": doc.get("formats", {}),
                    }
                )

            # Shape the output to match MongoDB's $group result structure
            return [
                {
                    "_id": {"company_id": company_id, "type": type_},
                    "captures": captures,
                }
                for (company_id, type_), captures in groups.items()
            ]

        grouped_captures = local_aggregate(collection, idSet)

    bucket = None
    if not CFG.IN_CLOUD and CFG.USE_GCP:
        from google.cloud.storage import Client

        googleClient = Client()
        bucket = googleClient.get_bucket(CFG.GCP_BUCKET)

    # Process one company and docType at a time
    for group in grouped_captures:
        companyId = group["_id"]["company_id"]
        docType = group["_id"]["type"]
        captures = group["captures"]

        # If there's only one capture in this set of documents no Change document can be made
        logger.info(f"Company ID: {companyId}, Type: {docType}...")
        logger.info(f"Number of captures: {len(captures)}...")
        if len(captures) <= 1:
            logger.error(
                f"No potential changes for company_id: {companyId}, type: {docType}"
            )
            continue

        # Ignore captures from the same date (this shouldn't happen, but won't display correctly on the webapp if it does)
        capturesToIgnore = set()

        for i in range(len(captures) - 1):
            if i in capturesToIgnore:
                continue

            olderCapture = captures[i]
            newerCapture = captures[i + 1]

            olderDate = olderCapture["date_fetched"].strftime("%Y-%m-%d")
            newerDate = newerCapture["date_fetched"].strftime("%Y-%m-%d")

            # If this document has two captures from the same day ignore the older one and flag it in the log
            if olderDate == newerDate:
                logger.error(
                    f"Detected Duplicate date for {olderCapture} and {newerCapture}."
                )
                logger.error(
                    "Ignoring the older version from this date.  Content should be validated immediately"
                )
                capturesToIgnore.add(i)

        validCaptures = [
            cap for idx, cap in enumerate(captures) if idx not in capturesToIgnore
        ]
        # Now that we have a list of valid captures, we can calculate total possible changes
        if len(validCaptures) <= 1:
            # After reducing to only valid captures, there was only one version
            logger.error(
                f"No valid potential changes for company_id: {companyId}, type: {docType}"
            )
            continue

        # Cache decoded document text within this group only, sized to this
        # group's own capture count. Any of these captures may be revisited
        # across the O(n^2) pairwise comparisons below, so caching all of
        # them avoids re-fetching/re-decoding the same text repeatedly -- but
        # the cache is rebuilt fresh per group (rather than shared globally
        # sized to the largest group in the whole run) so a company with a
        # long capture history doesn't keep its decoded text resident in
        # memory while later, unrelated groups are being processed.
        doc_cache = DocumentCache(capacity=len(validCaptures))
        # Separate cache for boundary-trimmed html text (see get_diff_text) --
        # kept apart from doc_cache since it stores a different derived value
        # (trimmed html-sourced text, not the raw .txt) under a different key
        # (the html path, not the txt path) for the same underlying capture.
        html_cache = DocumentCache(capacity=len(validCaptures))

        # Fetch every existing (older_id, newer_id) pair for this company/type
        # ONCE, rather than one find_one() per pair below -- the O(n^2) pairwise
        # loop was previously making that many individual round trips (e.g. 325
        # for a company with 26 captures), almost all of which just confirm a
        # change created in a prior run. A single query plus in-memory set
        # membership checks preserves identical behavior for a fraction of the
        # DB load.
        existingPairsQuery = {"company_id": companyId, "type": docType}
        if CFG.USE_MONGO:
            existingPairs = {
                (doc["older_document_id"], doc["newer_document_id"])
                for doc in mongoChangesDB.find(existingPairsQuery)
            }
        else:
            existingPairs = {
                (doc["older_document_id"], doc["newer_document_id"])
                for doc in local_find(localChangesDB, existingPairsQuery)
            }

        # Iterate through the documents in time order
        for i in range(len(validCaptures)):
            for j in range(i + 1, len(validCaptures)):
                olderCapture = validCaptures[i]
                newerCapture = validCaptures[j]

                changeExists = (
                    olderCapture["_id"],
                    newerCapture["_id"],
                ) in existingPairs

                if changeExists:
                    # This change does exist, we don't need to rebuild it
                    logger.info(
                        f"Change already exists for company_id: {companyId}, type: {docType}, older_capture: {olderCapture['_id']}, newer_capture: {newerCapture['_id']}..."
                    )
                else:
                    # This is a new change that needs to be created.
                    # The txt extension is always "txt" regardless of any historical drift in other formats.
                    # This is the canonical stored-capture path recorded in the change record below,
                    # independent of whichever text get_diff_text actually used to build the diff itself.
                    olderPath = f"{olderCapture['path']}.txt"
                    newerPath = f"{newerCapture['path']}.txt"

                    # Pull the display text -- boundary-trimmed html when
                    # CFG.USE_DIFF_TRIMMER allows it, otherwise the full
                    # untrimmed .txt -- from the GCP bucket.
                    olderText, newerText = get_diff_text(
                        olderCapture, newerCapture, bucket, doc_cache, html_cache
                    )
                    if olderText is None or newerText is None:
                        continue

                    # Create the diff data from the retrieved text content
                    diffData = create_patience_diff_data(olderText, newerText)

                    # Analyze the diff for any topic changes
                    addedText = "".join([item[0] for item in diffData if item[1]])
                    removedText = "".join([item[0] for item in diffData if item[2]])
                    topic_changes = analyze_topic_changes(addedText, removedText)

                    # Create version info string for the filename, ensure the timezone stays in utc
                    olderTimestamp = int(
                        olderCapture["date_fetched"]
                        .replace(tzinfo=timezone.utc)
                        .timestamp()
                    )
                    newerTimestamp = int(
                        newerCapture["date_fetched"]
                        .replace(tzinfo=timezone.utc)
                        .timestamp()
                    )
                    version_info = f"{olderTimestamp}-{newerTimestamp}"

                    # Create .CHANGES.json file path
                    changesFilePath = f"{CFG.CHANGES_DIR}/{companyId}/{docType}/{version_info}.CHANGES.json"

                    # Create .CHANGES.json file
                    change_data = {
                        "older_document": {
                            "id": str(olderCapture["_id"]),
                            "path": str(olderPath),
                            "date_fetched": olderCapture["date_fetched"]
                            .astimezone(timezone.utc)
                            .isoformat(),
                        },
                        "newer_document": {
                            "id": str(newerCapture["_id"]),
                            "path": str(newerPath),
                            "date_fetched": newerCapture["date_fetched"]
                            .astimezone(timezone.utc)
                            .isoformat(),
                        },
                        "topic_changes": topic_changes,
                        "diff": diffData,
                    }

                    # Upload to GCP
                    # Fix Windows path to be compatible with GCP
                    if not CFG.IN_CLOUD and CFG.USE_GCP:
                        changesData = json.dumps(change_data)
                        changesBlob = bucket.blob(changesFilePath)  # type: ignore
                        changesBlob.upload_from_string(
                            data=changesData, content_type="application/json"
                        )
                    else:
                        # Save the changes file locally
                        output_path = CFG.CAPTURES_DIR / changesFilePath
                        os.makedirs(output_path.parent, exist_ok=True)
                        with open(output_path, "w", encoding="utf-8") as f:
                            json.dump(change_data, f)

                    # Insert the changes info into the Changes collection to track it
                    change_document = {
                        "company_id": companyId,
                        "type": docType,
                        "older_document_id": olderCapture["_id"],
                        "newer_document_id": newerCapture["_id"],
                        "older_date_fetched": olderCapture["date_fetched"],
                        "newer_date_fetched": newerCapture["date_fetched"],
                        "path": changesFilePath,
                        "public_url": f"https://storage.googleapis.com/{CFG.GCP_BUCKET}/{changesFilePath}",
                    }
                    if CFG.USE_MONGO:
                        result = mongoChangesDB.insert_one(change_document)
                        logger.info(
                            f"  Changes detected: Inserted to MongoDB (ID: {result.inserted_id})"
                        )
                    else:
                        save_db([change_document], CFG.CHANGES_BSON)
                        logger.info("  Changes detected: Inserted to local database")

        # This group's doc_cache (and any diff data still referenced from the
        # inner loops above) goes out of scope here; force a collection pass
        # before moving to the next company/type group so a long capture
        # history doesn't linger in memory while unrelated groups are processed
        gc.collect()


if __name__ == "__main__":
    # Create .CHANGES.json files for all detected companies
    # This can be run as a standalone script
    # Use this if you need to repair the .CHANGES.json files or make changes to the diff format / calculation
    create_changes_json()
