# Transparency Archiver - Online Document Capture Tool
# This script creates the CHANGES.json file format used by the Comparison Tool on the webapp
# This file is designed to run within the sync, but can also be ran as a standalone script

# Imports
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
    local_find_one,
    save_db,
)

if CFG.USE_MONGO:
    from pymongo.collection import Collection


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

    for word, isAdded, isRemoved in wordDiff:
        if isRemoved:
            # Use special characters from past document
            if idx1 in sp1 and sp1[idx1]:
                result.append((sp1[idx1], False, False))
            result.append((word, isAdded, isRemoved))
            idx1 += 1
        elif isAdded:
            # Use special characters from new document
            if idx2 in sp2 and sp2[idx2]:
                result.append((sp2[idx2], False, False))
            result.append((word, isAdded, isRemoved))
            idx2 += 1
        else:
            # Unchanged word - prefer new special characters, fall back to old
            ws = sp2.get(idx2, sp1.get(idx1, ""))
            if ws:
                result.append((ws, False, False))
            result.append((word, isAdded, isRemoved))
            idx1 += 1
            idx2 += 1

    # Handle any trailing whitespace
    if idx1 in sp1:
        result.append((sp1[idx1], False, False))
    elif idx2 in sp2:
        result.append((sp2[idx2], False, False))

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
        collection = mongoClient[CFG.DB_NAME]["documents"]
        mongoChangesDB = mongoClient[CFG.DB_NAME]["changes"]
        # Mongo pipeline to structure the documents data for creating Change files
        pipeline = [
            {
                "$match": {
                    "format": "txt",
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
                        "format": "$format",
                    },
                    "captures": {
                        "$push": {
                            "_id": "$_id",
                            "path": "$path",
                            "date_fetched": "$date_fetched",
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
            # $match: filter by format and optionally by company_id
            matched = [
                doc
                for doc in documents
                if doc.get("format") == "txt"
                and (id_set is None or doc.get("company_id") in id_set)
            ]

            # $sort: sort by date_fetched ascending
            sorted_docs = sorted(matched, key=lambda d: d["date_fetched"])

            # $group: group by (company_id, type, format), pushing capture fields
            groups: dict[tuple, list] = {}
            for doc in sorted_docs:
                key = (doc.get("company_id"), doc.get("type"), doc.get("format"))
                groups.setdefault(key, []).append(
                    {
                        "_id": doc["_id"],
                        "path": doc.get("path"),
                        "date_fetched": doc["date_fetched"],
                    }
                )

            # Shape the output to match MongoDB's $group result structure
            return [
                {
                    "_id": {"company_id": company_id, "type": type_, "format": format_},
                    "captures": captures,
                }
                for (company_id, type_, format_), captures in groups.items()
            ]

        grouped_captures = local_aggregate(collection, idSet)

    bucket = None
    if not CFG.IN_CLOUD and CFG.USE_GCP:
        from google.cloud.storage import Client

        googleClient = Client()
        bucket = googleClient.get_bucket(CFG.GCP_BUCKET)

    max_captures = max((len(g["captures"]) for g in grouped_captures), default=10)
    doc_cache = DocumentCache(capacity=max_captures)

    # Process one company and docType at a time
    for group in grouped_captures:
        companyId = group["_id"]["company_id"]
        docType = group["_id"]["type"]
        format = group["_id"]["format"]
        captures = group["captures"]

        # If there's only one capture in this set of documents no Change document can be made
        logger.info(f"Company ID: {companyId}, Type: {docType}, Format: {format}...")
        logger.info(f"Number of captures: {len(captures)}...")
        if len(captures) <= 1:
            logger.error(
                f"No potential changes for company_id: {companyId}, type: {docType}, format: {format}"
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
                f"No valid potential changes for company_id: {companyId}, type: {docType}, format: {format}"
            )
            continue

        # Iterate through the documents in time order
        for i in range(len(validCaptures)):
            for j in range(i + 1, len(validCaptures)):
                olderCapture = validCaptures[i]
                newerCapture = validCaptures[j]

                # See if this Change document already exists
                query = {
                    "company_id": companyId,
                    "type": docType,
                    "older_document_id": olderCapture["_id"],
                    "newer_document_id": newerCapture["_id"],
                }
                if CFG.USE_MONGO:
                    changeExists = mongoChangesDB.find_one(query)
                else:
                    changeExists = local_find_one(localChangesDB, query)

                if changeExists:
                    # This change does exist, we don't need to rebuild it
                    logger.info(
                        f"Change already exists for company_id: {companyId}, type: {docType}, format: {format}, older_capture: {olderCapture['_id']}, newer_capture: {newerCapture['_id']}..."
                    )
                else:
                    # This is a new change that needs to be created
                    # Pull the data from the GCP bucket
                    olderPath = olderCapture["path"]
                    newerPath = newerCapture["path"]

                    # Check cache before fetching from GCS
                    olderText = doc_cache.get(olderPath)
                    if olderText is None:
                        logger.info(f"Cache miss — fetching {olderPath}")
                        olderText = decode_bytes(get_gcs_bytes(olderPath, bucket))
                        doc_cache.put(olderPath, olderText)
                    else:
                        logger.info(f"Cache hit — {olderPath}")

                    newerText = doc_cache.get(newerPath)
                    if newerText is None:
                        logger.info(f"Cache miss — fetching {newerPath}")
                        newerText = decode_bytes(get_gcs_bytes(newerPath, bucket))
                        doc_cache.put(newerPath, newerText)
                    else:
                        logger.info(f"Cache hit — {newerPath}")

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


if __name__ == "__main__":
    # Create .CHANGES.json files for all detected companies
    # This can be run as a standalone script
    # Use this if you need to repair the .CHANGES.json files or make changes to the diff format / calculation
    create_changes_json()
