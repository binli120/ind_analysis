@copyright filynai.com
@author: Bin Lee
@email: blee@filynai.com

# API Gateway HTTP Proxy for ECS

This Terraform module provisions an API Gateway HTTP API based on the local
OpenAPI document (`openapi-proxy.json.tpl`). Each path in the spec maps to the
FastAPI endpoints exposed by the pdf-analysis service. The API proxy is wired to
your ECS service via a VPC Link and ALB/NLB listener ARN. Terraform creates:

- A VPC link connected to the provided subnets + security groups.
- An HTTP API imported from the OpenAPI definition (with `x-amazon-apigateway-integration`
  blocks already pointing to the listener ARN).
- A stage (with optional access logs) and optional domain mapping.

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

1. Ensure the ECS service is fronted by an ALB/NLB listener ARN and that the listener’s security group allows traffic from the VPC link security group on the service port.
2. The VPC link subnets must reside in the same VPC as the load balancer and have routes to it.
3. If you need to attach a custom domain, create it separately (`aws_apigatewayv2_domain_name`) and pass the name via `custom_domain_name`.
4. The imported OpenAPI spec currently includes the endpoints `/analyze`, `/s3/upload-analyze`,
   `/s3/markdown`, `/s3/markdown/summary`, `/s3/markdown/save`, `/s3/analysis/status`, and
   `/s3/analysis/result`. Regenerate `openapi.json` (see repo root instructions) and update
   `openapi-proxy.json.tpl` if new endpoints are added.
5. After `terraform apply`, update DNS (Route53, etc.) to point to the API Gateway domain or custom domain mapping.
