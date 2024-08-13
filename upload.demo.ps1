# Stop the script if any command fails
$ErrorActionPreference = "Stop"

# --- CONFIGURATION ---
# Update these to match your project
$ProjectID   = "my-project-1234"
$Region      = "us-east4"
$Repository  = "document-capture"
$ImageName   = "document_capture"
$ImageTag    = "2.1"

# Construct the full Artifact Registry path
$FullImagePath = "${Region}-docker.pkg.dev/${ProjectID}/${Repository}/${ImageName}:${ImageTag}"

Write-Host "Target: $FullImagePath"

# 1. Authenticate Docker with Google Cloud
Write-Host "Authenticating Docker for $Region"
gcloud auth configure-docker "${Region}-docker.pkg.dev" --quiet

# 2. Build the Docker image locally
Write-Host "Building image ${ImageName}:${ImageTag}"
docker build -t "${ImageName}:${ImageTag}" .

# 3. Tag the image for Artifact Registry
Write-Host "Tagging image"
docker tag "${ImageName}:${ImageTag}" $FullImagePath

# 4. Push the image to Artifact Registry
Write-Host "Step 4: Pushing image to GAR"
docker push $FullImagePath

Write-Host "Successfully pushed $ImageName to Artifact Registry"