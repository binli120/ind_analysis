variable "aws_region" {
  type        = string
  description = "AWS region to deploy SNS topics into."
  default     = "us-east-1"
}

variable "name_prefix" {
  type        = string
  description = "Prefix applied to all SNS topics (e.g. project or environment identifier)."
}

variable "environment" {
  type        = string
  description = "Environment name for tagging (dev, staging, prod)."
  default     = "dev"
}

variable "tags" {
  type        = map(string)
  description = "Additional tags to apply to all SNS topics."
  default     = {}
}
