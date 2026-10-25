# Transparency Archiver - Online Document Capture Tool
# This file orchestrates the document capture process in parallel
# See the README for more information

# Imports
import concurrent.futures
import gc
from typing import Dict
import time

# Local Imports
from source.globals import CFG, logger
from source.utils import (
    mongo_connect,
    upload_to_GCP,
    update_Mongo,
    save_db,
    pull_db,
    notify_slack,
)
from source.crawler import BrowsertrixCrawler
from source.process import ProcessCrawl


class DocumentPipeline:
    """
    The class that manages the pipeline in parallel using concurrent threads

    Args:
      companyData (List): A list of company data structured to use unique urls as keys
    """

    def __init__(self, companyData: list):
        self.crawler = BrowsertrixCrawler()
        self.companyData = companyData
        self.maxWorkers = CFG.MAX_WORKERS

        # Initialize clients and buckets to be reused by threads
        self.mongoClient = None
        if CFG.USE_MONGO:
            logger.info("Connecting to MongoDB...")
            self.mongoClient = mongo_connect()
            self.collection = self.mongoClient[CFG.DB_NAME]["documents_v2"]
        else:
            self.collection = pull_db(CFG.DOCUMENTS_BSON)
        self.googleClient = None
        self.googleBucket = None
        if not CFG.IN_CLOUD and CFG.USE_GCP:
            from google.cloud.storage import Client

            logger.info("Connecting to Google Cloud Storage...")
            self.googleClient = Client()
            self.googleBucket = self.googleClient.bucket(CFG.GCP_BUCKET)
        logger.info("Connected to external services...")

    def process_document(
        self, companyData: Dict, batch_id: str
    ) -> tuple[str, str] | None:
        """
        Processes a single document through the entire capture and processing pipeline

        Args:
          companyData (Dict): A dictionary containing the company data to be processed.
          batch_id (str): A unique identifier for the batch of documents being processed.

        Returns:
          tuple (str, str): A tuple with the string url captured and the company_id of the capture
        """
        # Run the crawler on the document
        crawlResult = self.crawler.run_single_crawl(companyData, batch_id)

        # Check for a crawl failure and process the crawl result
        mongoData = []
        fileNames = []
        crawlUrls = []
        companyObjId = ""
        success = False
        for cResult in crawlResult:
            if cResult["success"]:
                success = True
                crawlUrls = cResult["urls"]
                companyObjId = cResult["objId"]
                mData, fNames = ProcessCrawl(
                    cResult, self.collection, self.googleBucket
                ).process_crawl()
                mongoData += mData
                fileNames += fNames
        # No crawls from this process succeeded, abort
        if not success:
            logger.error(
                f"Crawl Failed for {', '.join(companyData['urls'])}, skipping processing and not writing to recovery_log"
            )
            try:
                notify_slack(
                    f"Capture failed for {', '.join(companyData['urls'])}"
                )
            except Exception as e:
                logger.error(f"Failed to send slack message: {e}")
            return None

        # Processing has completed, log results
        logger.info(f"Processed {', '.join(companyData['urls'])}")
        logger.info(f"Crawl result: {crawlResult}")
        logger.info(f"Mongo data example: {mongoData[0] if mongoData else None}")
        logger.info(f"File name example: {fileNames[0] if fileNames else None}")

        if not CFG.DRY_RUN:
            if CFG.USE_MONGO:
                # Update the Mongo
                update_Mongo(mongoData, self.collection)
            else:
                # Update the local file
                save_db(mongoData, CFG.DOCUMENTS_BSON)

            # Upload to the GCP
            if not CFG.IN_CLOUD and CFG.USE_GCP:
                upload_to_GCP(fileNames, self.googleBucket)

            # Notify slack
            if len(mongoData) > 0:
                new_ids = [
                    m["company_id"] for m in mongoData if "txt" in m.get("formats", {})
                ]
                try:
                    # Only notify slack if the company has a new text capture
                    for pData in companyData["pValues"]:
                        if pData["objId"] in new_ids:
                            notify_slack(
                                f"New document version detected for {pData['name']}: {pData['pType']} ({', '.join(companyData['urls'])})"
                            )
                except Exception as e:
                    logger.error(f"Failed to send slack message: {e}")

        # Return the urls to write to the recovery file
        return crawlUrls, companyObjId

    def process_all_documents(self) -> set:
        """
        Processes a batch of documents through the entire capture and processing pipeline

        Returns:
          set: A set of companyIds that were captured by the archiver
        """
        results = set()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.maxWorkers
        ) as executor:
            futureToBatch = {
                executor.submit(
                    self.process_document,
                    {"urls": key, "pValues": value},
                    f"batch_{i + 1}_{int(time.time())}",
                    # Enumerate over the companyData to create one process per url group
                ): i
                for i, (key, value) in enumerate(self.companyData)
            }

            # Wait for all threads to complete
            for future in concurrent.futures.as_completed(futureToBatch):
                batch_idx = futureToBatch[future]
                try:
                    result = future.result()
                    # Update the recovery file here as each thread completes
                    if result:
                        # Unpack returned tuple
                        rUrls, rId = result
                        results.add(rId)
                        with open(CFG.LOG_DIR / "recovery_log.txt", "a") as f:
                            # Write every url in the group to the file
                            f.writelines(rUrl + "\n" for rUrl in rUrls)
                except MemoryError as e:
                    # With the RLIMIT_AS safeguard in place (source.globals.apply_memory_safeguard),
                    # this is expected to be what an oversized document looks like instead of the
                    # whole container getting OOM-killed -- log it distinctly so it's easy to tell
                    # apart from an actual bug, then let the other in-flight workers keep going
                    logger.error(
                        f"Batch {batch_idx + 1} hit the memory safeguard and was dropped: {e}"
                    )
                except Exception as e:
                    logger.error(f"Batch {batch_idx + 1} generated exception: {e}")
                finally:
                    # Encourage prompt release of this document's parsed DOM trees / PDF readers,
                    # which can involve reference cycles that outlive normal refcounting until a
                    # full GC pass -- worth the small fixed cost given multiple worker threads'
                    # peaks can otherwise overlap
                    gc.collect()

        if not CFG.IN_CLOUD and self.googleClient is not None:
            self.googleClient.close()
            logger.info("Disconnected main pipeline from Google Cloud Storage...")

        if self.mongoClient is not None:
            self.mongoClient.close()
            logger.info("Disconnected main pipeline from MongoDB...")

        return results
