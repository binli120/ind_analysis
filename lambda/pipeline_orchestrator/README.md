@copyright filynai.com
@author: Bin Lee
@email: blee@filynai.com

# Pipeline Orchestrator Lambda

This AWS Lambda function is triggered by S3 object creation events for PDF/Word
documents and by SNS completion notifications emitted from the downstream
pipeline modules. It coordinates the lifecycle of each uploaded document by
publishing the initial `ingest.completed` event and tracking per-stage
progress in DynamoDB.

## Responsibilities

1. **S3 Object Created (PDF/Word)**
   - Record or update the document entry in DynamoDB.
   - Publish an `ingest.completed` SNS message to kick off the pipeline.

2. **SNS `*_completed` Notifications**
   - Update the stored document status for the relevant stage.
   - Mark the overall document as completed when the final stage finishes.

## Environment Variables

| Variable | Description |
| -------- | ----------- |
| `PIPELINE_STATUS_TABLE` | DynamoDB table name used to persist document status. |
| `INGEST_COMPLETED_TOPIC_ARN` | SNS topic ARN that represents the `ingest.completed` notification. |
| `DEFAULT_COMPANY` | Fallback company identifier when not embedded in the object metadata. |
| `DEFAULT_PROJECT` | Fallback project identifier when not embedded in the object metadata. |
| `ALLOWED_EXTENSIONS` | Optional comma-separated list of extensions to monitor (default: pdf,doc,docx). |
| `STAGE_COMPLETION_TOPICS` | Optional mapping of stage names to completion topic ARNs when emitting derived events. |

## Local Development

- Install dependencies listed in `requirements.txt`.
- Package the Lambda using `zip -r function.zip app.py`.
- Deploy via Terraform (see `infra/terraform/pipeline_orchestrator`).
