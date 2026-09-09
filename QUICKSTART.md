# Transparency Archiver — Quickstart Guide

The Transparency Archiver is a document capture tool that tracks how apps and tech companies' legal and privacy policies change over time. It crawls policy URLs, stores snapshots, and can generate diff documents between versions.

> **Prerequisite:** Docker must be installed and running before you begin.

---

## How Configuration Works

The archiver uses two configuration files that serve distinct purposes:

- **`.env`** — Environment-level settings: connection strings, directory paths, GCP project info, and runtime flags like `DRY_RUN`. This file is gitignored and is where sensitive values like `MONGODB_URL` and `SLACK_WEBHOOK_URL` live. In Cloud Run, these values are set as environment variables and secrets on the job instead.

- **`config.yaml`** — Engine behavior settings: whether to use MongoDB or local files, worker counts, timeouts, capture priorities per company, and topic detection patterns. This file is safe to commit and is read by the engine at the path specified by `CONFIG_PATH` in your `.env`.

When in doubt about where a setting belongs, check the comments at the top of each file.

---

## Table of Contents

1. [Initial Setup](#1-initial-setup)
2. [Database Configuration](#2-database-configuration)
3. [Capture Storage Configuration](#3-capture-storage-configuration)
4. [Running the Capture Engine](#4-running-the-capture-engine)
   - [Local Development](#local-development)
   - [Docker](#docker)
   - [Google Cloud Run](#google-cloud-run)

---

## 1. Initial Setup

Clone the repository or download the latest release `.zip` and extract it:

```bash
git clone https://github.com/berkmancenter/transparency__archives.git
cd transparency__archives
```

Rename the included environment template:

```bash
cp .env.local .env
```

Open `.env` and fill in the values as you work through the steps below. The template includes comments explaining each option. Do not commit this file — it is gitignored for a reason.

---

## 2. Database Configuration

The archiver requires two core collections (or equivalent files): **`companies`** and **`documents`**.

- **`companies`** — Stores company names and the policy URLs to capture. This is the only collection you need to populate manually to get started.
- **`documents`** — Stores metadata about individual document snapshots. Managed automatically by the capture engine.

> If you plan to generate change documents between policy versions, you'll also need a **`changes`** collection/file.
> If you plan to host the companion web app, you'll also need a **`projects`** collection.

Choose one of the two backends below, then set the corresponding options in **`config.yaml`**.

---

### Option A: MongoDB (Recommended)

MongoDB is the recommended database backend. [MongoDB Compass](https://www.mongodb.com/products/compass) provides a convenient GUI for managing your data.

**Steps:**

1. Create a local (or hosted) MongoDB database.
2. Inside it, create the following collections:
   - `companies`
   - `documents`
   - `changes` *(optional — for diff generation)*
   - `projects` *(optional — for the web app)*
3. In **`.env`**, set `MONGODB_URL` to your connection string:
   ```env
   # Local MongoDB
   MONGODB_URL='mongodb://localhost:27017/'
   # MongoDB Atlas
   MONGODB_URL='mongodb+srv://user:password@cluster.mongodb.net/'
   ```
4. In **`config.yaml`**, set:
   ```yaml
   USE_MONGO: true
   DB_NAME: your_database_name
   ```
5. Add your first entry to the `companies` collection.

Each company document should look like this:

```json
{
  "id": "transparency-hub",
  "name": "Transparency Hub",
  "url": "https://hub.transparency.berkmancenter.org/",
  "doc_urls": {
    "Privacy Policy": {
      "name": "Privacy Policy",
      "urls": ["https://hub.transparency.berkmancenter.org/legal/privacy"]
    },
    "Terms of Use": {
      "name": "Terms of Use",
      "urls": ["https://hub.transparency.berkmancenter.org/legal/terms"]
    }
  }
}
```

---

### Option B: Local JSON/BSON Files

If you'd prefer not to set up MongoDB, the engine can run off local files.

**Steps:**

1. Create a `db/` folder inside the repo (or wherever you plan to run the engine).
2. Inside it, create:
   - `companies.json` — populated manually (see example below)
   - `documents.bson` — leave empty; managed automatically
   - `changes.bson` *(optional)*
3. In **`config.yaml`**, set:
   ```yaml
   USE_MONGO: false
   USE_LOCAL_COMPANIES: true
   COMPANIES_JSON: db/companies.json
   DOCUMENTS_JSON: db/documents.bson
   CHANGES_JSON: db/changes.bson
   ```

Your `companies.json` should be a JSON array following the same structure as the MongoDB example above:

```json
[
  {
    "_id": "123456789012345678901234", *(24 character Object id, must be unique per company)*
    "id": "transparency-hub",
    "name": "Transparency Hub",
    "url": "https://hub.transparency.berkmancenter.org/", *(optional)*
    "doc_urls": {
      "Privacy Policy": {
        "name": "Privacy Policy",
        "urls": ["https://hub.transparency.berkmancenter.org/legal/privacy"]
      },
      "Terms of Use": {
        "name": "Terms of Use",
        "urls": ["https://hub.transparency.berkmancenter.org/legal/terms"]
      }
    }
  }
]
```

---

## 3. Capture Storage Configuration

Choose where captured document snapshots will be stored, then set the corresponding options.

---

### Option A: Google Cloud Storage (Recommended for Production)

You'll need a GCP project with billing enabled. If you don't have one, [follow Google's getting started guide](https://cloud.google.com/docs/get-started).

**Steps:**

1. Create a GCS bucket.
2. In **`.env`**, set:
   ```env
   GCP_BUCKET='your-bucket-name'
   GCLOUD_PROJECT='your-project-id'
   TEMP_DIR='tmp'
   CAPTURES_DIR='tmp/docs'
   ```
3. In **`config.yaml`**, set:
   ```yaml
   USE_GCP: true
   ```
4. Authenticate your local environment:
   ```bash
   gcloud auth application-default login
   ```
   See [Google's auth documentation](https://cloud.google.com/docs/authentication/provide-credentials-adc) for details.

---

### Option B: Local Filesystem

**Steps:**

1. Create a folder to hold captured snapshots (e.g., `./captures/`).
   ```bash
   mkdir captures
   ```
2. In **`.env`**, set:
   ```env
   CAPTURES_DIR='./captures'
   TEMP_DIR='tmp'
   ```
3. In **`config.yaml`**, set:
   ```yaml
   USE_GCP: false
   ```

> **Important:** `CAPTURES_DIR` must **not** be a subfolder of `TEMP_DIR`. The engine uses `TEMP_DIR` for intermediate working files and deletes it at the end of a production run.

---

## 4. Running the Capture Engine

The capture engine wraps a custom fork of [Browsertrix Crawler](https://github.com/berkmancenter/transparency-hub-browsertrix-crawler), maintained by the Berkman Klein Center. You'll need this fork installed before running any capture mode.

### Installing the Browsertrix Fork

1. Ensure [Docker Engine](https://docs.docker.com/engine/install/) is installed and running.
2. Clone the repository or download the latest release of the BKC Browsertrix fork [here](https://github.com/berkmancenter/transparency-hub-browsertrix-crawler) and unzip it.

```bash
git clone https://github.com/berkmancenter/transparency-hub-browsertrix-crawler.git
cd transparency-hub-browsertrix-crawler
```

3. Build the Docker image within the `transparency-hub-browsertrix-crawler` repo/folder:

```bash
docker compose build
```

This creates a local image named `browsertrix-thub` that the capture engine will invoke during runs.

---

### Option A: Local Development

The capture engine can be run directly with Python. This release was built with **Python 3.13.3**.

1. Install dependencies:

```bash
pip install -r requirements.txt
# — or, if using pyproject.toml —
pip install .
```

2. Confirm your `.env` and `config.yaml` are configured, then run:

```bash
python main.py
```

> **Tip:** Set `DRY_RUN='true'` in your `.env` for a first run. The engine will process captures and save a local JSON copy of the results, but will not write to MongoDB or GCS, and will not delete `TEMP_DIR` afterward — making it easy to inspect output before committing to a full run.   

---

### Option B: Docker

The capture engine is also packaged to run via Docker Compose.  
There are a few configuration related variables within the `docker-compose.yml`.  
   If you want to save the log files locally, uncomment `# .logs:/app/logs`.
   If you want to save the captures locally, uncomment `# - ./db:/app/tmp/docs`, and update it to match your `CAPTURES_DIR`.
   If you are using local .json/.bson files for your metadata, uncomment `# - ./db:/app/db`, and update it to match the folder with your metadata files.

The `.env` file is preconfigured for Windows, setting `GCLOUD_CONFIG_DIR=${APPDATA}/gcloud`.  If you're on Linux or MacOS, set `GCLOUD_CONFIG_DIR` to `~/.config/gcloud`.

Run this command within the `transparency__archives` repo/folder.

```bash
docker compose up
```

This builds the capture engine image and immediately begins collecting snapshots using the values in your `.env` and `config.yaml`.

> **Tip:** Set `DRY_RUN='true'` in your `.env` before your first run to confirm everything is wired up correctly.

To capture additional companies, add entries to your `companies` collection or `.json` file and re-run.

---

### Option C: Google Cloud Run

Cloud Run is the recommended production deployment path. This mode requires a hosted MongoDB instance [See Part 2, Option A](#option-a-mongodb-recommended) (e.g., [MongoDB Atlas free tier](https://www.mongodb.com/atlas)) and a GCS bucket [See Part 3, Option A](#option-a-google-cloud-storage-recommended-for-production).

In Cloud Run, your `.env` values are provided as **environment variables and secrets** on the job — there is no `.env` file in the container. Your `config.yaml` is stored in GCS and read at runtime via the volume mount.

#### Step 1: Set Up Artifact Registry

You'll need to push two Docker images to Google's Artifact Registry: the Browsertrix fork and the capture engine.

Set up Artifact Registry by following [Google's documentation](https://cloud.google.com/artifact-registry/docs/docker/store-docker-container-images), then authenticate locally:

```bash
gcloud auth configure-docker
```

The file `upload.demo.ps1` (in this repo) and a similar file in the Browsertrix fork repo contain PowerShell scripts to push images automatically. Edit the following variables before running:

| Variable | Description |
|---|---|
| `$ProjectID` | Your GCloud project name |
| `$Region` | Your GCloud region |
| `$Repository` | Artifact Registry repository name |
| `$ImageName` | Name for the Docker image |
| `$ImageTag` | Tag for the image version |

---

#### Step 2: Create the Browsertrix Cloud Run Job

In the Google Cloud Console, create a new Cloud Run Job with the following settings:

- **Container image URL:** Your Artifact Registry image for `browsertrix-thub`
- **Container command:** `crawl`
- **Container arguments:** `--url`, `https://hub.transparency.berkmanklein.org/legal/privacy`, `--depth`, `0`
- **Volume:** Mount a GCS subfolder (e.g., `gs://your-bucket/crawls`) at `/crawls`
- **Memory:** 4 GiB
- **vCPUs:** 4
- **Timeout:** 15 minutes

---

#### Step 3: Create the Capture Engine Cloud Run Job

**Before creating the job:**

1. Fill out your `config.yaml` and upload it to your GCS bucket.
2. In **Google Secret Manager**, add secrets for any sensitive `.env` values:
   - Your MongoDB connection string (e.g., secret name: `MONGODB_URL`)
   - *(Optional)* Your Slack webhook URL (e.g., secret name: `SLACK_WEBHOOK_URL`)

**Create a new Cloud Run Job** with the following settings:

- **Container image URL:** Your Artifact Registry image for the capture engine
- **Volume:** Mount the full GCS bucket as a volume

**Set the following environment variables** (these replace your `.env` in Cloud Run):

| Variable | Equivalent `.env` key | Notes |
|---|---|---|
| `GCLOUD_PROJECT` | `GCLOUD_PROJECT` | Your GCloud project name |
| `GCLOUD_REGION` | — | Same region as the Browsertrix job |
| `GCLOUD_JOB` | — | Name of the Browsertrix Cloud Run Job from Step 2 |
| `GCP_BUCKET` | `GCP_BUCKET` | Name of the GCS bucket volume-mounted to this job |
| `TEMP_DIR` | `TEMP_DIR` | The GCS subfolder mounted on the Browsertrix job, relative to the volume mount |
| `CAPTURES_DIR` | `CAPTURES_DIR` | Where snapshots are written on the volume mount |
| `CHANGES_DIR` | `CHANGES_DIR` | Subdirectory within `CAPTURES_DIR` for change files |
| `SYNC_PRIORITY` | `SYNC_PRIORITY` | Set to `1` for a full run; higher values target higher-priority companies only |
| `CONFIG_PATH` | `CONFIG_PATH` | Path to `config.yaml` relative to the volume mount |

**Set the following secrets** (via Google Secret Manager):

| Variable | Notes |
|---|---|
| `MONGODB_URL` | Your MongoDB Atlas connection string |
| `SLACK_WEBHOOK_URL` | *(Optional)* Your Slack webhook URL |

> **Note:** `DRY_RUN` can also be set as a Cloud Run environment variable during testing. Remove it or set it to `false` for production runs.

> **Note:** Settings that live in `config.yaml` — such as `USE_MONGO`, `DB_NAME`, `USE_GCP`, `MAX_WORKERS`, `TIMEOUT`, `PRIORITY_DICT`, and `TOPIC_PATTERNS` — do not need to be set here. They are read from the file at `CONFIG_PATH` on the volume mount.

**Recommended resource settings:**

- **Memory:** 8 GiB
- **vCPUs:** 2
- **Timeout:** 12 hours *(adjust based on dataset size — lower is safer for cost control)*

---

## 5. Scheduling Runs with Cloud Scheduler

The `scheduler.demo.ps1` file sets up two Cloud Scheduler jobs that trigger the capture engine Cloud Run Job on a recurring basis, each with a different `SYNC_PRIORITY` override. The default schedule is:

| Scheduler Job | Schedule | `SYNC_PRIORITY` | Captures |
|---|---|---|---|
| `pipeline-weekly-friday` | Fridays at 12am ET | `1` | All non-excluded companies |
| `pipeline-daily-mon-thurs` | Mon–Thurs at 12am ET | `3` | Daily-priority companies only |

This maps directly to the `PRIORITY_DICT` in `config.yaml`: companies with a priority of `3` are captured every day, while all others are swept up in the weekly Friday run.

**Before running the script**, edit the configurable variables at the top:

| Variable | Description |
|---|---|
| `$PROJECT_ID` | Your GCloud project name |
| `$REGION` | Your GCloud region |
| `$JOB_NAME` | Name of the capture engine Cloud Run Job from Step 3 |
| `$SERVICE_ACCOUNT` | Service account email with permission to invoke the Cloud Run Job |
| `$TIMEZONE` | Timezone for the cron schedule (e.g., `America/New_York`) |
| `$SCHEDULE_WEEKLY` | Cron expression for the weekly run (default: Fridays at midnight) |
| `$SCHEDULE_DAILY` | Cron expression for the daily run (default: Mon–Thurs at midnight) |
| `$WEEKLY_PRIORITY` | `SYNC_PRIORITY` passed to the weekly run (default: `1`) |
| `$DAILY_PRIORITY` | `SYNC_PRIORITY` passed to the daily run (default: `3`) |

Then run the script:

```powershell
.\scheduler.demo.ps1
```

This creates both scheduler jobs via `gcloud`. If you need to update an existing job rather than create a new one, the script includes the corresponding `gcloud scheduler jobs update` commands as comments — swap them in for the `create` commands.

---

## Next Steps

Once your first capture run completes successfully, you can:

- **Generate diffs** between policy versions using the changes pipeline
- **Monitor runs** via Cloud Logging or Slack webhook notifications