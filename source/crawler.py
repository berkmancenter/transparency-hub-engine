# Transparency Archiver - Online Document Capture Tool
# This file pilots the image of Browsertrix Crawler THub
# See the README for more information
# Requires a running Docker image of browsertrix-thub, a custom fork of browsertrix-crawler by Webrecorder
# Configured to input a list of URLs and output a python Dict of crawl results
# These crawl results will then be parsed by process.py

# Imports
import concurrent.futures
import time
from typing import List, Dict
import sys
import os

# Local Imports
from source.globals import CFG, logger


class BrowsertrixCrawler:
    """
    The class that manages the custom fork of Browsertrix to run the crawls in the identified environment

    Args:
      dockerImage (str): The string name of the docker image of the custom browsertrix fork
    """

    def __init__(
        self,
        dockerImage: str = "browsertrix-thub:1.2",
        mode: str = "document_capture",
    ):
        self.dockerImage = dockerImage
        self.mode = mode

    def create_crawl_command(self, url: str, crawlName: str) -> List[str]:
        """ """
        if CFG.IN_LOCAL:
            # These options are only used when running this as a python script
            logger.info(f"Output directory: {os.path.abspath(CFG.CRAWL_DIR)}")
            cmd = [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{os.path.abspath(CFG.CRAWL_DIR)}:/crawls",  # Mount output directory
                "--name",
                f"crawl-{crawlName}-{int(time.time())}",  # Unique container name
                "-i",
                self.dockerImage,
            ]
        else:
            cmd = []

        # These are the base options for running the fork programmatically
        cmd = cmd + [
            "crawl",
            "--url",
            url,
            "--collection",
            crawlName,
            "--detachedHTML",  # Generate detached HTML (Custom Fork Param)
            "--behaviors",
            "autoscroll,autoplay,autofetch",  # Auto behaviors
            "--behavior-timeout",
            "30",
            "--timeout",
            "90",
        ]

        # With plans to add more modes, this will need to be expanded, but for now document_capture is the only valid mode
        if self.mode == "document_capture":
            # This includes some custom options, some of which  have been written into the browsertrix
            cmd = cmd + [
                "--generateWACZ",  # Generate WACZ format
                "--generatePDF",  # Generate PDF (Custom Fork Param)
                "--detachedText",  # Generate detached text (Custom Fork Param)
                "--screenshot",
                "fullPage",
                "--text",
                "to-pages",
                "--depth",
                "1",
                "--scopeType",
                "prefix",
                "--pageLimit",
                "20",
                # This will exclude links with query parameters, and reduce time and size
                # Some query parameters are irrelevant, haven't found an efficient way to exclude them
                # Hasn't been working as expected in testing
                # "--exclude", url + '\\?' # Exclude link changes from query parameters
            ]

        # Return the full generated command
        return cmd

    def run_single_crawl(self, pData: Dict, crawlName: str) -> List[Dict]:
        """ """
        url = pData["url"]
        pValues = pData["pValues"]

        cmd = self.create_crawl_command(url, crawlName)

        logger.info(f"Starting crawl '{crawlName}' with {url}")
        logger.debug(f"Command: {' '.join(cmd)}")

        startTime = time.time()

        # Each environment requires a different way to call the crawler container, with their own handling
        # Local uses subprocess.run
        if CFG.IN_LOCAL:
            try:
                import subprocess

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=CFG.TIMEOUT,  # 20 minute timeout
                )

                end_time = time.time()
                duration = end_time - startTime

                if result.returncode == 0:
                    logger.info(
                        f"Crawl '{crawlName}' completed successfully in {duration:.1f}s"
                    )
                    crawlDir = CFG.CRAWL_DIR / crawlName
                    outputFiles = (
                        list(crawlDir.glob("*.wacz")) if crawlDir.exists() else []
                    )

                    return [
                        {
                            "crawl_name": crawlName,
                            "success": True,
                            "url": url,
                            "objId": pValue["objId"],
                            "name": pValue["name"],
                            "pType": pValue["pType"],
                            "duration": duration,
                            "output_files": [str(f) for f in outputFiles],
                        }
                        for pValue in pValues
                    ]
                else:
                    logger.error(
                        f"Crawl '{crawlName}' failed with return code {result.returncode}"
                    )
                    logger.error(result.stdout)
                    logger.error(result.stderr)
                    return [
                        {
                            "crawl_name": crawlName,
                            "success": False,
                            "url": url,
                            "pValues": pValues,
                            "duration": duration,
                            "error": result.returncode,
                        }
                    ]
            except subprocess.TimeoutExpired:
                logger.error(f"Crawl '{crawlName}' timed out after 20 minutes")
                return [
                    {
                        "crawl_name": crawlName,
                        "success": False,
                        "url": url,
                        "pValues": pValues,
                        "error": "Timeout after 20 minutes",
                    }
                ]
            except Exception as e:
                logger.error(
                    f"Error running container for Crawl '{crawlName}'\n\tCrawl failed with exception: {e}"
                )
                return [
                    {
                        "crawl_name": crawlName,
                        "success": False,
                        "url": url,
                        "pValues": pValues,
                        "error": str(e),
                    }
                ]
        # Docker uses docker client
        elif CFG.IN_DOCKER:
            import docker  # type: ignore

            # Open a docker client to run the crawler
            client = docker.from_env()

            try:
                container = client.containers.run(
                    self.dockerImage,
                    name=f"crawl-{crawlName}-{int(time.time())}",
                    command=cmd,
                    volumes={"crawl-shared-data": {"bind": "/crawls", "mode": "rw"}},
                    remove=False,
                    detach=True,
                    shm_size="2g",
                )

                # Script will not make it this far if container runs into an error
                result = container.wait(timeout=CFG.TIMEOUT)  # 20 minute timeout

                endTime = time.time()
                duration = endTime - startTime

                logger.info(
                    f"Crawl '{crawlName}' completed successfully in {duration:.1f}s with exit code: {result['StatusCode']}"
                )
                crawl_dir = CFG.CRAWL_DIR / crawlName
                logger.info("Crawl saved at: " + str(crawl_dir))
                return [
                    {
                        "crawl_name": crawlName,
                        "success": True,
                        "url": url,
                        "objId": pValue["objId"],
                        "name": pValue["name"],
                        "pType": pValue["pType"],
                        "duration": duration,
                    }
                    for pValue in pValues
                ]
            except docker.errors.ContainerError as e:  # type: ignore
                endTime = time.time()
                duration = endTime - startTime

                logger.error(
                    f"Crawl '{crawlName}' failed with return code {e.exit_status}"
                )
                logger.error(
                    f"  Error: {e.stderr.decode() if e.stderr else 'No error output'}"
                )
                return [
                    {
                        "crawl_name": crawlName,
                        "success": False,
                        "url": url,
                        "pValues": pValues,
                        "duration": duration,
                        "error": e.exit_status,
                    }
                ]
            except Exception as e:
                logger.error(
                    f"Error running container for Crawl '{crawlName}'\n\tCrawl failed with exception: {e}"
                )
                return [
                    {
                        "crawl_name": crawlName,
                        "success": False,
                        "url": url,
                        "pValues": pValues,
                        "error": str(e),
                    }
                ]
        # Cloud uses google jobs client
        elif CFG.IN_CLOUD:
            from google.cloud import run_v2  # type: ignore

            # Open a google client to run the crawler
            client = run_v2.JobsClient()

            job_path = f"projects/{CFG.GCLOUD_PROJECT}/locations/{CFG.GCLOUD_REGION}/jobs/{CFG.GCLOUD_JOB}"

            overrides = {"container_overrides": [{"args": cmd}]}

            try:
                request = run_v2.RunJobRequest(name=job_path, overrides=overrides)

                operation = client.run_job(request=request)

                # Calling operation.result() blocks until the crawl job completes.
                # Bounded by CFG.TIMEOUT to match the local (subprocess.run) and
                # Docker (container.wait) branches above -- without a timeout, a
                # stuck Cloud Run job execution would hang this call forever.
                operation.result(timeout=CFG.TIMEOUT)

                endTime = time.time()
                duration = endTime - startTime

                logger.info(
                    f"Crawl '{crawlName}' completed successfully in {duration:.1f}s"
                )
                crawlDir = CFG.CRAWL_DIR / crawlName
                logger.info("Crawl saved at: " + str(crawlDir))

                return [
                    {
                        "crawl_name": crawlName,
                        "success": True,
                        "url": url,
                        "objId": pValue["objId"],
                        "name": pValue["name"],
                        "pType": pValue["pType"],
                        "duration": duration,
                    }
                    for pValue in pValues
                ]
            except concurrent.futures.TimeoutError:
                logger.error(f"Crawl '{crawlName}' timed out after {CFG.TIMEOUT}s")
                return [
                    {
                        "crawl_name": crawlName,
                        "success": False,
                        "url": url,
                        "pValues": pValues,
                        "error": f"Timeout after {CFG.TIMEOUT}s",
                    }
                ]
            except Exception as e:
                logger.error(
                    f"Error running container for Crawl '{crawlName}'\n\tCrawl failed with exception: {e}"
                )
                return [
                    {
                        "crawl_name": crawlName,
                        "success": False,
                        "url": url,
                        "pValues": pValues,
                        "error": str(e),
                    }
                ]
        else:
            sys.exit(1)
            raise Exception("Unknown environment")
