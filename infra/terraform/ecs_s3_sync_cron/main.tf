locals {
  module_arg = length(var.modules) > 0 ? "--modules ${join(",", [for m in var.modules : tostring(m)])}" : ""
  project_arg = length(var.projects) > 0 ? "--projects ${join(",", var.projects)}" : ""
  redis_arg   = trimspace(var.redis_url) != "" ? "--redis-url ${var.redis_url}" : ""
  ai_metadata_flag   = var.enable_ai_metadata ? "--ai-metadata" : ""
  ai_embeddings_flag = var.enable_ai_embeddings ? "--ai-embeddings" : ""

  base_args = compact([
    "--bucket ${var.s3_bucket}",
    "--company ${var.company}",
    project_arg != "" ? project_arg : null,
    module_arg != "" ? module_arg : null,
    redis_arg != "" ? redis_arg : null,
    ai_metadata_flag != "" ? ai_metadata_flag : null,
    ai_embeddings_flag != "" ? ai_embeddings_flag : null,
  ])

  command = trimspace("poetry run python scripts/s3_sync.py ${join(" ", concat(local.base_args, var.command_additional_args))}")

  container_environment = [
    for key, value in var.container_environment :
    {
      name  = key
      value = value
    }
  ]
}

resource "aws_cloudwatch_log_group" "s3_sync" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "s3_sync" {
  family                   = "${var.name_prefix}-s3-sync"
  cpu                      = var.cpu
  memory                   = var.memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([
    {
      name      = "s3-sync"
      image     = var.container_image
      essential = true
      entryPoint = [
        "/bin/sh",
        "-c"
      ]
      command = [
        local.command
      ]
      environment     = local.container_environment
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.s3_sync.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "s3-sync"
        }
      }
    }
  ])

  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "s3_sync" {
  name                = "${var.name_prefix}-s3-sync-schedule"
  description         = "Runs the PDF S3 sync pipeline on a cron schedule."
  schedule_expression = var.schedule_expression
  tags                = var.tags
}

data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "events_policy" {
  statement {
    actions   = ["ecs:RunTask"]
    resources = [aws_ecs_task_definition.s3_sync.arn]
  }

  statement {
    actions   = ["iam:PassRole"]
    resources = [var.execution_role_arn, var.task_role_arn]
  }
}

resource "aws_iam_role" "events_invoke" {
  name               = "${var.name_prefix}-s3-sync-events-role"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "events_invoke" {
  name   = "${var.name_prefix}-s3-sync-events-policy"
  role   = aws_iam_role.events_invoke.id
  policy = data.aws_iam_policy_document.events_policy.json
}

resource "aws_cloudwatch_event_target" "s3_sync" {
  rule      = aws_cloudwatch_event_rule.s3_sync.name
  target_id = "ecs-s3-sync"
  arn       = var.ecs_cluster_arn
  role_arn  = aws_iam_role.events_invoke.arn

  ecs_target {
    launch_type         = "FARGATE"
    platform_version    = var.platform_version
    task_definition_arn = aws_ecs_task_definition.s3_sync.arn
    task_count          = var.task_count

    network_configuration {
      subnets         = var.subnet_ids
      security_groups = var.security_group_ids
      assign_public_ip = var.assign_public_ip ? "ENABLED" : "DISABLED"
    }
  }
}
