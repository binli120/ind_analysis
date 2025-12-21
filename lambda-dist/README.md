@copyright filynai.com
@author: Bin Lee
@email: blee@filynai.com

Lambda build artifacts for the PDF analysis pipeline.

What this directory is for
- Stores packaged Lambda ZIPs under `lambda-dist/build`.
- Contains the source used to build the dispatcher Lambda under `lambda-dist/dispatcher`.

Dispatcher Lambda (SQS -> ECS)
The dispatcher Lambda reads SQS messages and starts an ECS Fargate task that runs:
`python -m scripts.pipeline_runner <payload-json>`.

Required environment variables
- `ECS_CLUSTER`: ECS cluster name or ARN.
- `ECS_TASK_DEFINITION`: task definition family or ARN.
- `ECS_SUBNETS`: comma-separated subnet IDs.
- `ECS_SECURITY_GROUPS`: comma-separated security group IDs.
- `DB_ENABLED`: optional, currently unused but reserved for future behavior.

Build the ZIP locally
From repo root:
```
mkdir -p lambda-dist/dispatcher/build
python -m venv lambda-dist/dispatcher/.venv
source lambda-dist/dispatcher/.venv/bin/activate
pip install -r lambda-dist/dispatcher/requirements.txt -t lambda-dist/dispatcher/build
cp lambda-dist/dispatcher/sqs_dispatcher.py lambda-dist/dispatcher/build/
cd lambda-dist/dispatcher/build
zip -r ../sqs_dispatcher.zip .
```

Deploy
- Upload `lambda-dist/build/sqs_dispatcher.zip` (or `lambda-dist/dispatcher/sqs_dispatcher.zip`)
  to AWS Lambda as the function package.
- Set the handler to `sqs_dispatcher.handler`.
- Attach the SQS trigger and ensure IAM permissions allow `ecs:RunTask`.

Notes
- This folder is for deployment artifacts; local development runs from `scripts/` and `src/`.
