variable "name_prefix" {
  description = "Prefix used for naming ECS and CloudWatch resources."
  type        = string
}

variable "aws_region" {
  description = "AWS region where resources are deployed."
  type        = string
}

variable "ecs_cluster_arn" {
  description = "ECS cluster ARN where the scheduled task will run."
  type        = string
}

variable "execution_role_arn" {
  description = "IAM role assumed by ECS agent for pulling images and writing logs."
  type        = string
}

variable "task_role_arn" {
  description = "IAM role assumed by the running container."
  type        = string
}

variable "container_image" {
  description = "ECR image URI that contains the pdf-analysis code and dependencies."
  type        = string
}

variable "container_environment" {
  description = "Additional environment variables injected into the container."
  type        = map(string)
  default     = {}
}

variable "subnet_ids" {
  description = "Private subnet IDs used by the scheduled task."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security group IDs applied to the task ENI."
  type        = list(string)
}

variable "assign_public_ip" {
  description = "Whether to assign a public IP (only for public subnets)."
  type        = bool
  default     = false
}

variable "cpu" {
  description = "CPU units for the Fargate task."
  type        = string
  default     = "1024"
}

variable "memory" {
  description = "Memory for the Fargate task."
  type        = string
  default     = "2048"
}

variable "schedule_expression" {
  description = "CloudWatch cron/rate expression controlling when the sync runs."
  type        = string
  default     = "cron(0 9 * * ? *)"
}

variable "platform_version" {
  description = "Fargate platform version."
  type        = string
  default     = "1.4.0"
}

variable "task_count" {
  description = "How many tasks to launch per trigger."
  type        = number
  default     = 1
}

variable "s3_bucket" {
  description = "Bucket scanned by scripts/s3_sync.py."
  type        = string
}

variable "company" {
  description = "Company folder prefix (passed to --company)."
  type        = string
  default     = "filynai.com"
}

variable "projects" {
  description = "Projects included in the sync."
  type        = list(string)
  default     = ["LT1009"]
}

variable "modules" {
  description = "Module numbers to include."
  type        = list(number)
  default     = [1, 2, 3, 4, 5]
}

variable "redis_url" {
  description = "Redis connection string."
  type        = string
}

variable "enable_ai_metadata" {
  description = "Toggle --ai-metadata flag."
  type        = bool
  default     = true
}

variable "enable_ai_embeddings" {
  description = "Toggle --ai-embeddings flag."
  type        = bool
  default     = true
}

variable "command_additional_args" {
  description = "Extra CLI arguments appended to s3_sync.py."
  type        = list(string)
  default     = []
}

variable "log_group_name" {
  description = "CloudWatch log group for the container."
  type        = string
  default     = "/ecs/pdf-analysis-s3-sync"
}

variable "log_retention_days" {
  description = "Retention period for CloudWatch logs."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default     = {}
}
