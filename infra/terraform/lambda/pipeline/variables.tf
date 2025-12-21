variable "region" {
  description = "AWS region"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for naming resources"
  type        = string
}

variable "source_bucket" {
  description = "S3 bucket name to watch for uploads"
  type        = string
}

variable "s3_to_sqs_package" {
  description = "Path to ZIP package for s3_to_sqs Lambda"
  type        = string
}

variable "sqs_worker_package" {
  description = "Path to ZIP package for sqs_worker Lambda"
  type        = string
}

variable "default_tenant_id" {
  description = "Default tenant UUID for inserts"
  type        = string
  default     = null
}

variable "default_user_id" {
  description = "Default user UUID for created_by"
  type        = string
  default     = null
}

variable "enable_langchain" {
  description = "Enable LangChain extraction in worker"
  type        = string
  default     = "true"
}

variable "llm_model" {
  description = "LLM model name"
  type        = string
  default     = "gpt-4o-mini"
}

variable "database_url" {
  description = "Postgres connection string for NCDRepository"
  type        = string
  sensitive   = true
}

variable "openai_api_key" {
  description = "OpenAI API key when LangChain is enabled"
  type        = string
  sensitive   = true
  default     = null
}

variable "queue_visibility_timeout" {
  description = "SQS visibility timeout in seconds"
  type        = number
  default     = 300
}

variable "queue_retention" {
  description = "SQS message retention in seconds"
  type        = number
  default     = 86400
}

variable "worker_timeout" {
  description = "Lambda timeout for sqs_worker"
  type        = number
  default     = 300
}

variable "worker_memory" {
  description = "Lambda memory for sqs_worker (MB)"
  type        = number
  default     = 2048
}

variable "worker_batch_size" {
  description = "Max messages per batch for sqs_worker"
  type        = number
  default     = 1
}
