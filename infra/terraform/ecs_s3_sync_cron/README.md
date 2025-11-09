# ECS Scheduled S3 Sync

This Terraform module wires up an EventBridge (CloudWatch) cron rule that fires an
on‑demand Fargate task to run `scripts/s3_sync.py` inside your pdf-analysis image.
Use it when you deploy this repository to ECS but still want the markdown/metadata
pipeline to run on a schedule.

## What it creates

- A dedicated CloudWatch log group for job output.
- An ECS task definition that executes:

  ```
  poetry run python scripts/s3_sync.py \
    --bucket ${var.s3_bucket} \
    --company ${var.company} \
    --projects LT1009 \
    --modules 1,2,3,4,5 \
    --redis-url redis://localhost:6379/0 \
    --ai-metadata \
    --ai-embeddings
  ```

  The project/module lists and other CLI flags are configurable by module variables.
- An EventBridge rule plus IAM role that starts the task on the cron schedule you
  provide.

## Usage

```hcl
module "s3_sync_cron" {
  source = "../ecs_s3_sync_cron"

  name_prefix        = "pdf-analysis"
  aws_region         = "us-east-1"
  ecs_cluster_arn    = aws_ecs_cluster.pdf_analysis.arn
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.pdf_analysis_task.arn
  container_image    = "${aws_ecr_repository.pdf_analysis.repository_url}:latest"

  subnet_ids         = module.vpc.private_subnet_ids
  security_group_ids = [aws_security_group.ecs_tasks.id]

  schedule_expression = "cron(0 10 * * ? *)" # 10:00 UTC daily
  s3_bucket           = "doc-repository-dev"
  company             = "filynai.com"
  projects            = ["LT1009"]
  modules             = [1, 2, 3, 4, 5]
  redis_url           = "redis://cache.internal:6379/0"

  container_environment = {
    OPENAI_API_KEY         = var.openai_api_key
    SUPABASE_URL           = var.supabase_url
    SUPABASE_SERVICE_KEY   = var.supabase_service_key
  }

  tags = {
    Project = "pdf-analysis"
    Owner   = "blee"
  }
}
```

Change `schedule_expression` to any valid EventBridge cron/rate expression (the module
defaults to `cron(0 0 * * ? *)`, i.e. midnight UTC). If the task needs internet
egress (for S3, Redis, or OpenAI) be sure the subnets and security groups you pass can
reach those endpoints (via NAT, VPC endpoints, etc.).
