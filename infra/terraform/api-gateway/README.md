# API Gateway HTTP Proxy for ECS

This Terraform module provisions an API Gateway HTTP API that forwards traffic to the pdf-analysis service running on ECS behind an Application Load Balancer listener. It creates:

- An `aws_apigatewayv2_api` (HTTP) with a `$default` route.
- A `aws_apigatewayv2_vpc_link` wired to the provided subnets and security groups.
- A proxy `aws_apigatewayv2_integration` targeting the ALB listener ARN.
- A stage with optional CloudWatch access logs.
- An optional API mapping for an existing custom domain.

## Usage

```hcl
module "pdf_analysis_api_gateway" {
  source  = "../../infra/terraform/api-gateway"
  aws_region                     = "us-east-1"
  api_name                       = "pdf-analysis-api"
  stage_name                     = "prod"
  alb_listener_arn               = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/pdf-analysis-alb/..."
  vpc_link_subnet_ids            = ["subnet-123", "subnet-456"]
  vpc_link_security_group_ids    = ["sg-123abc"]
  tags = {
    Project = "pdf-analysis"
    Env     = "prod"
  }
}
```

### Required Inputs

- `alb_listener_arn`: Listener ARN for the load balancer in front of the ECS service.
- `vpc_link_subnet_ids`: Private subnet IDs that can reach the ALB.
- `vpc_link_security_group_ids`: Security groups permitting traffic to the ALB listener.

### Optional Inputs

- `stage_name`: Defaults to the provided string (no default for visibility).
- `api_description`: Description shown in the AWS console.
- `custom_domain_name`: Map to an existing API Gateway custom domain.
- `enable_access_logs`, `access_log_group_name`, `access_log_retention_days`, `access_log_format`: Configure CloudWatch access logging.
- `tags`: Merge-in additional resource tags.

### Outputs

- `api_endpoint`: HTTPS invoke URL for the deployed stage.
- `api_execution_arn`: Execution ARN, useful for IAM permissions on invoking roles.
- `api_id`: ID of the API.
- `vpc_link_id`: ID of the VPC Link for troubleshooting.

## Deployment Notes

1. Ensure the ALB listener security group allows traffic from the VPC link security group on port 8000 (or whichever port the ECS service exposes).
2. The VPC link subnets must reside in the same VPC as the ALB and have private routing to it.
3. If you need to attach a custom domain, create the domain separately (`aws_apigatewayv2_domain_name`) and pass its name via `custom_domain_name`.
4. Update DNS (Route53, etc.) to point to the API Gateway domain once the mapping is provisioned.
