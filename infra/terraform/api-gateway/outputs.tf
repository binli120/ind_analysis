output "api_id" {
  description = "ID of the created API Gateway HTTP API."
  value       = aws_apigatewayv2_api.this.id
}

output "api_execution_arn" {
  description = "Execution ARN for the API, useful for IAM policies."
  value       = aws_apigatewayv2_api.this.execution_arn
}

output "api_endpoint" {
  description = "Invoke URL for the deployed API stage."
  value       = aws_apigatewayv2_stage.this.invoke_url
}

output "vpc_link_id" {
  description = "ID of the API Gateway VPC link used to reach the ECS load balancer."
  value       = aws_apigatewayv2_vpc_link.this.id
}
