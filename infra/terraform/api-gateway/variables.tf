variable "aws_region" {
  description = "AWS region where the API Gateway and VPC link resources will be created."
  type        = string
}

variable "api_name" {
  description = "Name for the API Gateway HTTP API."
  type        = string
}

variable "stage_name" {
  description = "Stage name for the API Gateway deployment (e.g. prod, staging)."
  type        = string
}

variable "protocol_type" {
  description = "API Gateway protocol type. Typically HTTP for ECS services behind an ALB."
  type        = string
  default     = "HTTP"
}

variable "api_description" {
  description = "Optional description for the API Gateway."
  type        = string
  default     = "Public entrypoint for the pdf-analysis ECS service."
}

variable "alb_listener_arn" {
  description = "ARN of the Application Load Balancer listener that fronts the ECS service."
  type        = string
}

variable "vpc_link_subnet_ids" {
  description = "Subnet IDs used by the API Gateway VPC link to reach the ALB."
  type        = list(string)
}

variable "vpc_link_security_group_ids" {
  description = "Security group IDs attached to the API Gateway VPC link ENIs."
  type        = list(string)
}

variable "integration_timeout_ms" {
  description = "Timeout in milliseconds for the VPC link integration."
  type        = number
  default     = 29000
}

variable "enable_access_logs" {
  description = "Whether to enable access logging for the API stage."
  type        = bool
  default     = true
}

variable "access_log_group_name" {
  description = "Optional CloudWatch log group name override for API access logs."
  type        = string
  default     = null
}

variable "access_log_retention_days" {
  description = "Retention in days for the access log CloudWatch log group."
  type        = number
  default     = 30
}

variable "access_log_format" {
  description = "JSON format string for API Gateway access logs."
  type        = string
  default     = jsonencode({
    requestId      = "$context.requestId"
    requestTime    = "$context.requestTime"
    httpMethod     = "$context.httpMethod"
    path           = "$context.path"
    status         = "$context.status"
    integration    = "$context.integrationErrorMessage"
    responseLatency = "$context.responseLatency"
    ip             = "$context.identity.sourceIp"
    userAgent      = "$context.identity.userAgent"
  })
}

variable "custom_domain_name" {
  description = "Optional API Gateway custom domain to map the stage to."
  type        = string
  default     = null
}

variable "tags" {
  description = "Common tags to apply to all created resources."
  type        = map(string)
  default     = {}
}
