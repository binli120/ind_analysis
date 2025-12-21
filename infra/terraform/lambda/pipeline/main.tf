terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  lambda_role_name = "${var.name_prefix}-lambda-role"
  queue_name       = "${var.name_prefix}-pipeline-queue"
  bucket_arn       = "arn:aws:s3:::${var.source_bucket}"
}

resource "aws_iam_role" "lambda" {
  name = local.lambda_role_name
  assume_role_policy = jsonencode({
    Version : "2012-10-17",
    Statement : [{
      Action    : "sts:AssumeRole",
      Effect    : "Allow",
      Principal : { Service : "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version : "2012-10-17",
    Statement : [
      {
        Effect : "Allow",
        Action : [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        Resource : "*"
      },
      {
        Effect : "Allow",
        Action : [
          "s3:GetObject",
          "s3:GetObjectVersion"
        ],
        Resource : "${local.bucket_arn}/*"
      },
      {
        Effect : "Allow",
        Action : [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ],
        Resource : aws_sqs_queue.pipeline.arn
      }
    ]
  })
}

resource "aws_sqs_queue" "pipeline" {
  name                      = local.queue_name
  visibility_timeout_seconds = var.queue_visibility_timeout
  message_retention_seconds  = var.queue_retention
}

resource "aws_lambda_function" "s3_to_sqs" {
  function_name = "${var.name_prefix}-s3-to-sqs"
  role          = aws_iam_role.lambda.arn
  handler       = "s3_to_sqs.handler"
  runtime       = "python3.11"
  timeout       = 30
  filename      = var.s3_to_sqs_package
  source_code_hash = filebase64sha256(var.s3_to_sqs_package)

  environment {
    variables = {
      PIPELINE_QUEUE_URL = aws_sqs_queue.pipeline.id
      DEFAULT_TENANT_ID  = var.default_tenant_id
      DEFAULT_USER_ID    = var.default_user_id
    }
  }
}

resource "aws_lambda_function" "sqs_worker" {
  function_name = "${var.name_prefix}-sqs-worker"
  role          = aws_iam_role.lambda.arn
  handler       = "sqs_worker.handler"
  runtime       = "python3.11"
  timeout       = var.worker_timeout
  memory_size   = var.worker_memory
  filename      = var.sqs_worker_package
  source_code_hash = filebase64sha256(var.sqs_worker_package)

  environment {
    variables = {
      DEFAULT_TENANT_ID  = var.default_tenant_id
      DEFAULT_USER_ID    = var.default_user_id
      ENABLE_LANGCHAIN   = var.enable_langchain
      LLM_MODEL          = var.llm_model
      DATABASE_URL       = var.database_url
      OPENAI_API_KEY     = var.openai_api_key
    }
  }
}

resource "aws_s3_bucket_notification" "s3_trigger" {
  bucket = var.source_bucket

  lambda_function {
    lambda_function_arn = aws_lambda_function.s3_to_sqs.arn
    events              = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_lambda_permission.allow_s3]
}

resource "aws_lambda_permission" "allow_s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.s3_to_sqs.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = local.bucket_arn
}

resource "aws_lambda_event_source_mapping" "sqs_trigger" {
  event_source_arn = aws_sqs_queue.pipeline.arn
  function_name    = aws_lambda_function.sqs_worker.arn
  batch_size       = var.worker_batch_size
}
