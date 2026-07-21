# Transparency Archiver - Online Document Capture Tool

The Transparency Archiver is a browser-based web archiving tool.

The Transparency Archiver powers the [Transparency Hub](https://hub.transparency.berkmancenter.org)

---

## Summary

- [About](#about)
- [Main Features](#main-features)
- [Development](#development)

---

## About  

This is a document capture pipeline written in Python designed to be used with a custom locally hosted docker fork of Browsertrix Crawler.

This tool hopes to provide accessible document capturing for researchers, journalists, and interested consumers.  

By creating a capture of a document, you can then cross-reference changes across the document's life cycle.

The Transparency Archiver is intended to work with a document store, locally or cloud hosted, that can then hold these captures, and can identify when new versioning might've occured.

These captures also help to make the web data more accessible by providing static formats.

It is recommended to run the Transparency Archiver as a docker image, but it is functional in a local python environment or can be hosted on Google Cloud Run.

---

It is designed to read data from a MongoDB (preferred) or .json file and retrieve current versions of documents from the urls in the data.

We aim to store each document in 5 formats: txt, json, html, pdf, and wacz.
Our data is stored in a MongoDB instance in 3 collections: `companies`, `documents`, and `changes`.  

Example `company` database entry:

```bson
{
  "_id": {
    "$oid": "675211559efd527760c64413"
  },
  "id": "platform",
  "name": "Platform",
  "doc_urls": {
    "Terms of Service": "https://www.platform.com/terms",
    "Privacy Policy": "http://www.platform.com/privacy",
    "Community Guidelines": "https://platform.com/community-standards",
    "Transparency Report": "https://platform.com/transparency"
  },

  # Below are Optional attributes

  "url": "https://www.platform.com/",
  "url_wiki": "https://en.wikipedia.org/wiki/Platform",
  "metadata": { # Optional
    "sns_type": "socialization",
    "year_launched": 2000,
    "provider": "company"
  }
}
```

Example `document` database entry:

```bson
{
  "_id": {
    "$oid": "67784259cef22046ad5f727c"
  },
  "company_id": "675211559efd527760c64414",
  "path": "company_id/type/unix_timestamp.txt",
  "type": "community_guidelines",
  "date_fetched": {
    "$date": "2025-01-03T20:02:32.024Z"
  },
  "public_url": "https://storage.googleapis.com/document_store/company_id/type/unix_timestamp.format",
  "original_url": "https://company.com/type",
  "format": "txt"
}
```

### MongoDB Compass ([Mongo website](https://www.mongodb.com/compass))

Transparency Archiver - Online Document Capture Tool is designed to work with a Mongo Database to maintain company and document information across multiple captures, although it can be configured to use local .json files.

Download MongoDB Compass so you can view & engage with the data in a friendly UI.  You'll need to get a local instance running and then set your environment variable to point to it.
To run the local instance, create "C:\data\db" then cd to "C:\Program Files\MongoDB\Server\8.0\bin" and run `mongod`

`export MONGODB_URL='mongodb://localhost:27017/'`

To view the front end, you'll need node.js and npm (see transparency_hub repo)

### Google Cloud Platform Bucket

Transparency Archiver - Online Document Capture Tool is designed to work with a Google Storage bucket for Snapshot storage, although it can be configured to use your local filesystem instead.

Check out their [getting started guide](https://docs.cloud.google.com/docs/get-started) and their info on [creating a storage bucket](https://docs.cloud.google.com/storage/docs)
If you are running any code that engages with a GCP bucket, you'll also want to set `export GCP_BUCKET='document_store'`
For setting up a local environment, you'll need to [authenticate with google](https://cloud.google.com/docs/authentication/set-up-adc-local-dev-environment)

## Main Features

The Transparency Archiver can create captures in the following formats:

### WACZ

The .wacz or .warc.gz format is an internet archive, capturing as much data as possible about the connection, and can include multiple webpages as well as web content important for HTML rendering.

Using replayweb.page or similar tools these archives can be replayed and investigated with high fidelity, maintaining the user experience of the document as well as its legal content.  

The HTML data, and consequently text data, can be retrieved from these captures if you're interested in performing your own data cleaning.

### PDF

The .pdf format is provided for primary target urls in the Transparency Archiver to enable a simple non-embedded viewing experience.  The .pdf format enables easy printing of the captures alongside relevant graphics found on the webpage.

### HTML

The .html of the primary target url is provided as a closer-to-text format that preserves content the .txt format might have missed.  It can also be parsed to remove unwanted text material from a capture, such as detection of content from headers, footers, or pop-ups.  

### TXT

The .txt format is provided for easy data access, parsing, and comparison.  This is a simple text parse of the client-side html at time of capture, and may include non-document content such as headers, footers, or pop-ups.

### Other formats

The Transparency Archiver also includes a .warc.json format that includes text content of all urls captured, not just the primary url.  This format automatically reduces duplicate captures.

The .CHANGES.json format is a change object based on the patience difference algorithm.  This format will only be generated if the Transparency Archiver has access to a previous capture of the relevant document.  Using this format you can programmatically highlight the difference between document versions, or see when specific text was added or removed.  This format is also capable of checking the text for changed patterns, which can highlight high-level version changes.  

---

### Running a DB sync: main.py

The way we make production DB updates is by running the main.py script. This script reads through the `companies` database, fetches the current content at the listed `doc_urls`, compares it to the most recent snapshot of that policy document for that company (per format type), and then creates a new archive if they differ.  This script is designed to automatically update the MongoDB and GCP Storage Bucket it is given access to.

#### Running in Docker

This repo is set up to run as a docker image.  If you have Docker installed and running, you can use `docker-compose build` and `docker-compose up` to run the sync.

This pipeline requires a built image of our [custom fork of BrowsertrixCrawler](https://github.com/berkmancenter/transparency-hub-browsertrix-crawler) available in your Docker engine.  See that repo for build instructions.

#### Running Locally

This repo can be run locally with a user-installation of Python 3.13.3.  It still requires a built image of our [custom fork of BrowsertrixCrawler](https://github.com/berkmancenter/transparency-hub-browsertrix-crawler) available in your Docker engine.  See that repo for build instructions.

This script offers the following customization options from the command line:
`--dry_run` will not save outputs to external connections, will not delete temporary files from runtime. (default: false)
Accepts true or false.

`--temp_dir` directs the program to use a specific LOCAL folder for its temporary data for downloads and processing. (default: tmp)
Accepts any valid Path.

`--captures_dir` directs the program where to store the captures.  In a local runtime, this should be within the temp_dir so that it is cleaned up at the end of a sync. (default: tmp/docs)
Accepts any valid Path.

`--changes_dir` directs the program where to store change data within the captures_dir.  These will not be created if --dry_run is set to true. (default: changes)
Accepts any valid Path.

`--log_dir` directs the program where to store log data and a recovery file that holds data in case the sync unexpectedly crashes.  This should not be within the temp_dir. (default: logs)"
Accepts any valid Path.

`--mongodb_url` the mongo connection string to connect to the MongoDB collections. (default: mongodb://localhost:27017/)
Accepts a mongo connection string.  See MongoDB documentation for more details.

`--gcp_bucket` the name of the Google Cloud Platform bucket to store new snapshots collected during the sync. (default: my-bucket-1234)

`--gcloud_project` the name of the Google Cloud Platform project that holds your storage bucket. (default: my-project-1234)

`--gcloud_region` the region of your Google Cloud Platform bucket. (default: us-east4)

`--gcloud_job` the Google Cloud Platform identifier for the Cloud Job of your custom BrowsertrixCrawler fork (default: my-job-1234)

`--sync_priority` the priority level to run the sync at, highly configurable using the PRIORITY_DICT in the config.  When set to 1, will run on all companies not disabled in the config. (default: 1)
Accepts any positive integer, though generally should be between 1-4 depending on your configuration setup.

`--config` the Path to the configuration file. (default: config.yaml)
Accepts any valid Path.

#### Running on Google Cloud Platform

The sync can be packaged to the Google Cloud Platform Artifact Registry if enabled on your project.  See `upload.demo.ps1` for guidelines on how to automatically push the sync to this Artifact Registry.  You'll also need to upload the config file to your Google Cloud Platform Storage Bucket, and will need to have a hosted MongoDB instance accessible with a connection string.  

The sync job can then be configured with the following options:

Recommended Memory is 8 GiB to handle the processing of .warc files, and 2 vCPUs for optimal process handling.  

The Google Cloud Platform Storage Bucket needs to be mounted as a volume for the Container.

GCLOUD_PROJECT should be set to the name of your Google Cloud Platform Project this sync and storage bucket is hosted on.

TEMP_DIR should be set within the mount path of the Storage Bucket. (Recommended: /mnt/runtime/data)

DOC_DIR should be set within the TEMP_DIR. (Recommended: /mnt/runtime/data/docs)

GCLOUD_REGION should be set to the region of your Google Cloud Platform Storage Bucket.

GCLOUD_JOB should be set to the name of the BrowsertrixCrawler fork job also set up within Google Cloud Run.

CAPTURES_DIR should be set as the direct mount path of the Storage Bucket. (Recommended: /mnt)

CONFIG_PATH should be set wherever you store the config file within your Storage Bucket.  This must be uploaded manually as part of setup. (Recommended: /mnt/runtime/config.yaml)

SYNC_PRIORITY can be set to whatever positive integer is appropriate. (Recommended: 1)

GCP_BUCKET is the name of the mounted Storage Bucket.

LOG_DIR should be set within the mount path of the Storage Bucket. (Recommended: /mnt/runtime/logs)

MONGODB_URL should be established as a secret using the Google Cloud Platform Secret Manager.  This cannot use the local connection string.  

## Related Repositories

This project is part of the Transparency Hub ecosystem maintained by the Berkman Klein Center for Internet & Society at Harvard University:

| Repository | Description |
| ----------- | ------------- |
| **[Transparency Hub](https://github.com/berkmancenter/transparency-hub)** | Next.js frontend — the public-facing website |
| **[Transparency Archiver](https://github.com/berkmancenter/transparency-hub-engine)** | Python pipeline that uses this crawler to archive policy documents |
| **[Browsertrix Crawler Fork](https://github.com/berkmancenter/transparency-hub-browsertrix-crawler)** (this repo) | Custom fork of Browsertrix Crawler |

## Development

### Pre-commit hooks

Install dev dependencies and activate the hooks once after cloning:

```bash
pip install -r requirements-dev.txt
pre-commit install
```

This runs `ruff format` and `ruff check --fix` automatically on every commit.

### Linting (manual)

`ruff format && ruff check --fix`

### Testing

The test suite lives in `tests/` and uses `pytest`. Install it alongside the runtime dependencies:

`pip install -r requirements.txt && pip install pytest pytest-cov`

Then run the suite from the repo root:

`pytest`

To see a coverage report:

`pytest --cov --cov-report=term-missing`

All external dependencies (MongoDB, Google Cloud Storage, the Browsertrix Crawler docker/subprocess/Cloud Run invocations, Slack webhooks, the filesystem) are faked or mocked, so the suite has no network, database, or docker requirements and never touches your real `.env`, `tmp/`, `logs/`, or `db/` directories. It also strips the handful of environment variables this codebase reads (`MONGODB_URL`, `GCP_BUCKET`, etc.) before each test, so a developer's local `.env` credentials never leak into test assertions.

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards.

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0).

## Acknowledgments

- Developed by the [Berkman Klein Center for Internet & Society](https://cyber.harvard.edu/)
- A project of the [Applied Social Media Lab](https://asml.cyber.harvard.edu/) initiative
