# Transparency Archiver - Online Document Capture Tool
# This function serves as the entry point for the document capture process
# See the README for more information

# Imports
import sys

# Local Imports
from source.globals import CFG, logger
from source.utils import initialize_sync, get_validated_companies, format_data, cleanup
from source.pipeline import DocumentPipeline
from source.changes import create_changes_json


def main():
    # Import company data from mongo or json
    logger.info("Retrieving company data from document store")
    companyData = get_validated_companies()

    # Format company data from the Mongo structure into a format that can be used by our pipeline
    logger.info("Restructing company data into collated url-based objects")
    formattedData = format_data(companyData)

    # Send mongo data to pipeline wrapper
    logger.info("Starting Processing Pipeline")
    pipeline = DocumentPipeline(formattedData)

    # Run the pipeline
    idSet = pipeline.process_all_documents()

    # Create the .CHANGES.json to reflect any new text documents
    if not CFG.DRY_RUN and CFG.CREATE_CHANGES:
        logger.info(
            "Creating Change documents and storing their information on the Mongo"
        )
        create_changes_json(idSet)

    # Recursively delete the TEMP_DIR directory as well as the recovery state from the runtime
    if not CFG.DRY_RUN:
        logger.info("Deleting temporary runtime data")
        cleanup()

    # If we made it this far, then everything was successful
    logger.info("Document capture process completed successfully, closing script...")
    sys.exit(0)


if __name__ == "__main__":
    # Initialize the sync, declare any configuration values, and create required directories
    initialize_sync()

    # Run the main function
    main()
