output "task_definition_arn" {
  description = "ARN of the scheduled S3 sync task definition."
  value       = aws_ecs_task_definition.s3_sync.arn
}

output "event_rule_name" {
  description = "Name of the CloudWatch Events rule driving the cron schedule."
  value       = aws_cloudwatch_event_rule.s3_sync.name
}

output "log_group_name" {
  description = "CloudWatch Logs group that stores job output."
  value       = aws_cloudwatch_log_group.s3_sync.name
}
