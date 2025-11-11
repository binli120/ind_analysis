terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "archive_file" "lambda_package" {
  type        = "zip"
  source_dir  = var.lambda_source_dir
  output_path = "${path.module}/build/pipeline_orchestrator.zip"
}

resource "aws_iam_role" "lambda_role" {
  name               = "${var.name_prefix}-pipeline-orchestrator-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = var.tags
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "lambda_policy" {
  statement {
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${var.aws_region}:${var.aws_account_id}:*"]
  }

  statement {
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:GetItem",
    ]
    resources = [aws_dynamodb_table.pipeline_status.arn]
  }

  statement {
    actions   = ["sns:Publish"]
    resources = concat([var.ingest_completed_topic_arn], var.completion_topic_arns)
  }
}

resource "aws_iam_role_policy" "lambda_inline" {
  name   = "${var.name_prefix}-pipeline-orchestrator-policy"
  role   = aws_iam_role.lambda_role.id
  policy = data.aws_iam_policy_document.lambda_policy.json
}

resource "aws_dynamodb_table" "pipeline_status" {
  name         = var.pipeline_status_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "document_id"

  attribute {
    name = "document_id"
    type = "S"
  }

  tags = merge(var.tags, {
    Component = "pipeline-orchestrator"
  })
}

resource "aws_lambda_function" "pipeline_orchestrator" {
  function_name    = "${var.name_prefix}-pipeline-orchestrator"
  filename         = data.archive_file.lambda_package.output_path
  source_code_hash = data.archive_file.lambda_package.output_base64sha256
  handler          = "app.handler"
  runtime          = "python3.11"
  role             = aws_iam_role.lambda_role.arn
  timeout          = 60
  memory_size      = 512

  environment {
    variables = {
      PIPELINE_STATUS_TABLE      = aws_dynamodb_table.pipeline_status.name
      INGEST_COMPLETED_TOPIC_ARN = var.ingest_completed_topic_arn
      DEFAULT_COMPANY            = var.default_company
      DEFAULT_PROJECT            = var.default_project
      ALLOWED_EXTENSIONS         = join(",", var.allowed_extensions)
    }
  }

  tags = var.tags
}

resource "aws_lambda_permission" "allow_s3_invoke" {
  statement_id  = "AllowExecutionFromS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pipeline_orchestrator.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = "arn:aws:s3:::${var.ingest_bucket_name}"
}

resource "aws_s3_bucket_notification" "ingest_notifications" {
  bucket = var.ingest_bucket_name

  lambda_function {
    lambda_function_arn = aws_lambda_function.pipeline_orchestrator.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".pdf"
  }

  lambda_function {
    lambda_function_arn = aws_lambda_function.pipeline_orchestrator.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".doc"
  }

  lambda_function {
    lambda_function_arn = aws_lambda_function.pipeline_orchestrator.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".docx"
  }

  depends_on = [aws_lambda_permission.allow_s3_invoke]
}

resource "aws_lambda_permission" "allow_sns_invoke" {
  for_each = toset(var.completion_topic_arns)

  statement_id  = "AllowExecutionFromSNS-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pipeline_orchestrator.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = each.value
}

resource "aws_sns_topic_subscription" "completion_subscriptions" {
  for_each = toset(var.completion_topic_arns)

  topic_arn = each.value
  protocol  = "lambda"
  endpoint  = aws_lambda_function.pipeline_orchestrator.arn

  depends_on = [aws_lambda_permission.allow_sns_invoke]
}
