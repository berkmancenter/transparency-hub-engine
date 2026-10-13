# Wayback Machine Historical Backfill - Cloud Run Job image
#
# Built with the REPO ROOT as build context (not this directory), because it
# needs two things from outside this folder that it deliberately doesn't
# duplicate: the shared source/ package (text reconstruction, quality
# classifiers, Mongo/GCS helpers, .CHANGES.json generation) and the trained
# classifier models/ it loads. Build from the repo root:
#
#   docker build -f archive_backfill/Dockerfile -t archive-backfill .
#
# See archive_backfill/upload.ps1 for the Artifact Registry push, and the
# repo README's "Wayback Backfill Job" section for `gcloud run jobs deploy`.

FROM python:3.14-slim

# Required for the Google Cloud Run environment's GCS FUSE volume mount.
RUN mkdir -p /mnt && chmod 777 /mnt

WORKDIR /app

# Install this job's own dependencies (a superset of what the shared
# source/ modules it imports need, plus `requests`).
COPY archive_backfill/requirements.txt ./archive_backfill/requirements.txt
RUN pip install --no-cache-dir -r archive_backfill/requirements.txt

# Shared production package and trained classifier models, reused as-is.
COPY source/ ./source/
COPY models/ ./models/

# This job's own code.
COPY archive_backfill/ ./archive_backfill/

USER root

ENTRYPOINT ["python", "archive_backfill/main.py"]
