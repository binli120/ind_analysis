locals {
  input_topic_arns      = { for stage, topic in aws_sns_topic.stage_input : stage => topic.arn }
  completion_topic_arns = { for stage, topic in aws_sns_topic.stage_completion : stage => topic.arn }
}

output "input_topic_arns" {
  description = "SNS topic ARNs for stage inputs keyed by stage identifier."
  value       = local.input_topic_arns
}

output "completion_topic_arns" {
  description = "SNS topic ARNs for stage completion events keyed by stage identifier."
  value       = local.completion_topic_arns
}

output "all_topic_arns" {
  description = "Combined list of all SNS topic ARNs."
  value = concat(
    values(local.input_topic_arns),
    values(local.completion_topic_arns),
  )
}

output "stage_successors" {
  description = "Map of each stage to the downstream stages it should trigger."
  value       = local.stage_successors
}

output "stage_successor_arns" {
  description = "Downstream stage input topic ARNs keyed by stage identifier."
  value       = local.stage_successor_arns
}
