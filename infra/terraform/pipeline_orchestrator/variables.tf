variable "aws_region" {
  type        = string
  description = "AWS region for deployment."
}

variable "aws_account_id" {
  type        = string
  description = "AWS account ID used for IAM resource ARNs."
}

variable "name_prefix" {
  type        = string
  description = "Prefix applied to created resources."
}

variable "tags" {
  type        = map(string)
  description = "Common tags applied to resources."
  default     = {}
}

variable "pipeline_status_table_name" {
  type        = string
  description = "Name of the DynamoDB table storing document status."
}

variable "ingest_completed_topic_arn" {
  type        = string
  description = "SNS topic ARN that receives ingest.completed events."
}

variable "completion_topic_arns" {
  type        = list(string)
  description = "SNS topic ARNs that emit *_completed notifications."
  default     = []
}

variable "ingest_bucket_name" {
  type        = string
  description = "Name of the S3 bucket containing uploaded documents."
}

variable "lambda_source_dir" {
  type        = string
  description = "Path to the Lambda source directory."
}

variable "default_company" {
  type        = string
  description = "Fallback company identifier inserted into ingest events."
  default     = "unknown-company"
}

variable "default_project" {
  type        = string
  description = "Fallback project identifier inserted into ingest events."
  default     = "default-project"
}

variable "allowed_extensions" {
  type        = list(string)
  description = "File extensions that trigger the Lambda from S3 events."
  default     = ["pdf", "doc", "docx"]
}
