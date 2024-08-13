# Transparency Archiver - Online Document Capture Tool
# This file includes the ProcessCrawl class, which manages local document processing for the Archiver

# Imports
from pathlib import Path
from datetime import datetime, timezone
import json
import shutil

# Local Imports
from .globals import CFG, logger
from .utils import join_path, local_find_one
from .async_compare import compare_files, validate_file

if CFG.USE_MONGO:
    from pymongo.collection import Collection


class ProcessCrawl:
    """
    The class that manages the local document processing after the document capture is complete

    Args:
      crawlResult (dict): A single crawl result dictionary that lists details about the capture and document
      collection (object): A reference to the Mongo Document collection, or the contents of the local database
      googleBucket (object | None): A reference to the Document Storage Bucket
    """

    def __init__(
        self,
        crawlResult: dict,
        collection: Collection | list[dict],
        googleBucket: object | None = None,
    ):
        # Initialize class constants
        self.collection = collection
        self.googleBucket = googleBucket
        self.crawlResult = crawlResult

        # Establish variables based on crawl
        self.cDir = CFG.CRAWL_DIR / "collections" / self.crawlResult["crawl_name"]
        self.aDir = Path(self.cDir, "archive")
        self.jDir = Path(self.cDir, "pages")
        # Save the retrieved formats at sDir
        self.sDir = (
            CFG.CAPTURES_DIR / self.crawlResult["objId"] / self.crawlResult["pType"]
        )
        self.sDir.mkdir(parents=True, exist_ok=True)
        self.cTimestamp = self.crawlResult["crawl_name"].split("_")[-1]
        cDateObj = datetime.fromtimestamp(int(self.cTimestamp), tz=timezone.utc)
        self.genItemInfo = {
            "company_id": self.crawlResult["objId"],
            "path": f"{self.crawlResult['objId']}/{self.crawlResult['pType']}/{self.cTimestamp}",
            "type": self.crawlResult["pType"],
            "date_fetched": cDateObj,
            "public_url": f"https://storage.googleapis.com/{CFG.GCP_BUCKET}/{self.crawlResult['objId']}/{self.crawlResult['pType']}/{self.cTimestamp}",
            "original_url": self.crawlResult["url"],
        }

        # Initialize variables that update during process_crawl
        self.confirmedNew = False
        self.failCounter = 0
        self.results = []
        self.fileNames = []

    def process_crawl(self):
        """
        The wrapper for processFormat that manages the different formats and directories

        Returns:
          tuple (list, list): A list of dictionaries formated for the Document collection and a list of filenames to upload to the storage bucket
        """

        # List of tuples that include the file format ending and the local file directory
        formatDir = [
            ("txt", self.aDir),
            ("warc.json", self.jDir),
            ("html", self.aDir),
            ("pdf", self.aDir),
        ]
        for formatObj in formatDir:
            # Do the actual processing dependent on the formatObj
            self.process_format(formatObj)

        # Process the wacz file last.  If all other formats failed, skip it
        # Note that this would include a failure of the warc.json, which can serve as a point of truth for even the archive
        # The wacz should always be saved if another format changed
        if self.failCounter < len(formatDir):
            self.process_format(("wacz", self.cDir))
        else:
            logger.info("All other formats failed, skipping wacz file")

        # All processing is complete, return itemInfo objects and file names
        return self.results, self.fileNames

    def process_format(self, formatObj: tuple):
        """
        For each file format: Finds the local file, verifies it is a valid and updated capture, and moves it to the captures directory

        Args:
          formatObj (tuple (str, Path)): A tuple containing a string of the file format ending and a Path to the local directory to search for the captured file
        """
        iFormat, iDir = formatObj

        try:
            # Find the format type file in iDir
            if iFormat == "warc.json":
                # Parse two files, pages.jsonl and extraPages.jsonl
                # Find the .jsonl file in iDir
                pagesJsonFile = iDir / "pages.jsonl"
                extraPagesJsonFile = iDir / "extraPages.jsonl"
                cTypeFile = join_path(iDir, f"{self.cTimestamp}.warc.json")

                # Write temporary file locally inside crawl directory
                with open(cTypeFile, "w", encoding="utf-8") as f:
                    json.dump(
                        self.create_warc_json(pagesJsonFile, extraPagesJsonFile), f
                    )
            else:
                cTypeFile = list(iDir.glob(f"*.{iFormat}"))
                if cTypeFile:
                    cTypeFile = cTypeFile[0]
                else:
                    raise Exception(f"No {iFormat} file created during capture")

            # Write format specific information into a copy of itemInfo
            itemInfo = self.genItemInfo.copy()
            itemInfo["format"] = f"{iFormat}"
            # WACZ files are refered to by their unzipped name, WARC, in the format field of the Document collection
            if iFormat == "wacz":
                itemInfo["format"] = "warc"
            itemInfo["path"] += f".{iFormat}"
            itemInfo["public_url"] += f".{iFormat}"

            # Verify the file has changes, bypass for wacz file
            if (
                iFormat == "wacz"
                or self.confirmedNew
                or self.verify_content(itemInfo, cTypeFile)
            ):
                # Confirmed that this is a new capture
                logger.info(
                    f"Found new {iFormat} file for {itemInfo['original_url']}, will add capture data to database..."
                )

                # Copy the file into sDir and add capture info to return lists
                newFilePath = join_path(self.sDir, f"{self.cTimestamp}.{iFormat}")
                shutil.copy(cTypeFile, newFilePath)
                self.results.append(itemInfo)
                self.fileNames.append(newFilePath)

                if iFormat == "txt":
                    # Set confirmed_new since the .txt should be an accurate indicator across all other existing formats
                    self.confirmedNew = True
            else:
                logger.info(f"No new {iFormat} file for {itemInfo['original_url']}")
                self.failCounter += 1
        except Exception as e:
            # Continue to try to find other formats
            logger.error(
                f"Failed to find/copy {iFormat} file for {self.genItemInfo['original_url']}: {e}"
            )
            self.failCounter += 1

    def create_warc_json(self, pagesFile: Path, extraPagesFile: Path):
        """
        Converts the pages.jsonl and extrapages.jsonl files generated by Browsertrix Crawler into a unified and collated structure

        Args:
          pagesFile (Path): The path to the pages.jsonl file generated by the capture
          extraPagesFile (Path): The path to the extrapages.jsonl file generated by the capture
        """

        jsondata = []
        # Load the base page file into memory
        with open(pagesFile, "r", encoding="utf-8") as f:
            # Parse the jsonl file into a dictionary
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    # Ignore blank lines
                    continue
                try:
                    jsondata.append(json.loads(line))
                except json.JSONDecodeError as e:
                    # Attempt to skip invalid lines
                    logger.error(f"JSON decode error on line {line_num}: {e}")
                    continue
        # Load the extra page file into memory
        with open(extraPagesFile, "r", encoding="utf-8") as f:
            # Parse the jsonl file into a dictionary
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    # Ignore blank lines
                    continue
                try:
                    jsondata.append(json.loads(line))
                except json.JSONDecodeError as e:
                    # Attempt to skip invalid lines
                    logger.error(f"JSON decode error on line {line_num}: {e}")
                    continue

        # Create a list of unique urls and texts
        uniqueUrls = set()
        uniqueTexts = set()

        records = []
        # Parse the page data looking for duplicate content across multiple urls
        for page in jsondata:
            if not page.get("url") or not page.get("text"):
                continue
            elif page["url"] not in uniqueUrls and page["text"] not in uniqueTexts:
                uniqueUrls.add(page["url"])
                uniqueTexts.add(page["text"])
                # Non-duplicate, add a new record
                records.append({"urls": [page["url"]], "content": page["text"]})
            elif page["text"] in uniqueTexts:
                # Duplicate text, add url to existing record
                for record in records:
                    if record["content"] == page["text"]:
                        # Collate duplicate content pages into the same record
                        record["urls"].append(page["url"])
                        logger.info(f"Found duplicate content urls at {record['urls']}")
                        break

        # Create the warc.json file
        warcJson = {
            "total_unique_content": len(uniqueTexts),
            "total_urls": len(uniqueUrls),
            "records": records,
        }
        return warcJson

    def verify_content(self, item: dict, cPath: Path) -> bool:
        """
        A wrapper function for compare.py that checks to see if a prior version of this document can be located in the existing document store, and then analyzes them for changes
        It also validates the file to ensure it matches the expectations of a successful captured document

        Args:
          item (dict): A dictionary of capture relevant item info in the same structure as the Document collection
          cPath (Path): A Path to the local file containing the new capture in the specified format

        Returns:
          bool: True if the document passed validation and is a new document or updates were found when compared to the prior version, otherwise False
        """

        # Do a simple quality check on the capture
        if not validate_file(cPath, item["format"]):
            logger.info(
                f"Failed to validate content for {item['original_url']}, rejecting capture"
            )
            return False

        # Match company_id, type, and format, and return the most recent capture
        query = {
            "company_id": item["company_id"],
            "type": item["type"],
            "format": item["format"],
        }
        sort = [("date_fetched", -1)]
        if CFG.USE_MONGO:
            bestMatch = self.collection.find_one(query, sort=sort)  # type: ignore
        else:
            bestMatch = local_find_one(self.collection, query, sort=sort)  # type: ignore
        if bestMatch is None:
            # No previous capture was found, save this capture
            logger.info(
                f"No previous capture found for {item['original_url']}, okay to push to Mongo and GCP..."
            )
            return True
        else:
            try:
                # Compare the local file with the GCP file using compare.py
                return compare_files(
                    cPath, bestMatch["path"], item["format"], self.googleBucket
                )
            except Exception as e:
                # If comparison fails, do not push the new capture, and log failure
                logger.error(
                    f"Compare funcion failed for {item['format']} file for {item['original_url']}: {e}"
                )
                logger.error(
                    "This file will not be saved to the document store or database"
                )
                logger.error(
                    "If this issue is reoccuring, it must be resolved to save this document"
                )
                return False
