output "queue_url" {
  value       = aws_sqs_queue.pipeline.id
  description = "SQS queue URL"
}

output "s3_to_sqs_function_arn" {
  value       = aws_lambda_function.s3_to_sqs.arn
  description = "ARN of the S3-to-SQS Lambda"
}

output "sqs_worker_function_arn" {
  value       = aws_lambda_function.sqs_worker.arn
  description = "ARN of the SQS worker Lambda"
}
