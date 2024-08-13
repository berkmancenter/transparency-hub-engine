FROM python:3.14-slim

# This line is required for the Google Cloud Run environment
RUN mkdir -p /mnt/data && chmod 777 /mnt/data

WORKDIR /app

# Install Docker CLI (this is ignored in Google Cloud Run environment)
RUN apt-get update && \
    apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

USER root

# Run the application, default to a dry run
ENTRYPOINT ["python", "main.py"]