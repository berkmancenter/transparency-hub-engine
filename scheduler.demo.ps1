# ============================================================
# GCP Cloud Scheduler Setup for Pipeline
# ============================================================
 
# --- Configurable Variables ---------------------------------
 
$PROJECT_ID         = "my-project-1234"
$REGION             = "us-east4"
$JOB_NAME           = "document-capture"
$SERVICE_ACCOUNT    = "my-service-account@my-project-1234.iam.gserviceaccount.com"
$TIMEZONE           = "America/New_York"
 
# Schedule times (cron format)
$SCHEDULE_WEEKLY    = "0 0 * * 5"      # Friday 12am ET
$SCHEDULE_DAILY     = "0 0 * * 1-4"    # Mon-Thurs 12am ET
 
# Scheduler job names
$SCHEDULER_WEEKLY   = "pipeline-weekly-friday"
$SCHEDULER_DAILY    = "pipeline-daily-mon-thurs"
 
# Environment variable overrides per schedule, these change the SYNC_PRIORITY value
# See the config for more details on how the SYNC_PRIORITY value is used
$WEEKLY_PRIORITY    = "1"
$DAILY_PRIORITY     = "3"
 
# ------------------------------------------------------------
$ErrorActionPreference = "Stop"
 
$BASE_URI = "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/$JOB_NAME`:run"
 
# --- Build message bodies -----------------------------------
# Using temp files to avoid PowerShell serialization dropping quotes in JSON
 
$WEEKLY_BODY = "{`"overrides`":{`"containerOverrides`":[{`"env`":[{`"name`":`"SYNC_PRIORITY`",`"value`":`"$WEEKLY_PRIORITY`"}]}]}}"
$DAILY_BODY  = "{`"overrides`":{`"containerOverrides`":[{`"env`":[{`"name`":`"SYNC_PRIORITY`",`"value`":`"$DAILY_PRIORITY`"}]}]}}"
 
$WEEKLY_BODY | Out-File -FilePath "$env:TEMP\weekly_body.json" -Encoding utf8
$DAILY_BODY  | Out-File -FilePath "$env:TEMP\daily_body.json"  -Encoding utf8
 
# --- Create Weekly (Friday) Scheduler -----------------------
 
Write-Host "Creating weekly Friday scheduler..."

# gcloud scheduler jobs update http $SCHEDULER_WEEKLY `
gcloud scheduler jobs create http $SCHEDULER_WEEKLY `
    --project=$PROJECT_ID `
    --schedule=$SCHEDULE_WEEKLY `
    --time-zone=$TIMEZONE `
    --uri=$BASE_URI `
    --message-body-from-file="$env:TEMP\weekly_body.json" `
    --oauth-service-account-email=$SERVICE_ACCOUNT `
    --location=$REGION
 
# --- Create Daily (Mon-Thurs) Scheduler ---------------------
 
Write-Host "Creating daily Mon-Thurs scheduler..."

# gcloud scheduler jobs update http $SCHEDULER_DAILY `
gcloud scheduler jobs create http $SCHEDULER_DAILY `
    --project=$PROJECT_ID `
    --schedule=$SCHEDULE_DAILY `
    --time-zone=$TIMEZONE `
    --uri=$BASE_URI `
    --message-body-from-file="$env:TEMP\daily_body.json" `
    --oauth-service-account-email=$SERVICE_ACCOUNT `
    --location=$REGION
 
Write-Host "Done! Schedulers created successfully."