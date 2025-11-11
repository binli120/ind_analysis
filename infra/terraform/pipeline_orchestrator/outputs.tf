output "lambda_function_name" {
  description = "Name of the pipeline orchestrator Lambda function."
  value       = aws_lambda_function.pipeline_orchestrator.function_name
}

output "lambda_function_arn" {
  description = "ARN of the pipeline orchestrator Lambda function."
  value       = aws_lambda_function.pipeline_orchestrator.arn
}

output "pipeline_status_table_name" {
  description = "Name of the DynamoDB table used for pipeline status."
  value       = aws_dynamodb_table.pipeline_status.name
}

output "pipeline_status_table_arn" {
  description = "ARN of the DynamoDB table used for pipeline status."
  value       = aws_dynamodb_table.pipeline_status.arn
}
