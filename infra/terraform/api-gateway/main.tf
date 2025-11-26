terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  count             = var.enable_access_logs ? 1 : 0
  name              = coalesce(var.access_log_group_name, "/aws/apigateway/${var.api_name}")
  retention_in_days = var.access_log_retention_days
  tags              = var.tags
}

locals {
  openapi_body = templatefile("${path.module}/openapi-proxy.json.tpl", {
    api_name               = jsonencode(var.api_name)
    api_description        = jsonencode(var.api_description)
    connection_id          = aws_apigatewayv2_vpc_link.this.id
    integration_uri        = var.alb_listener_arn
    integration_timeout_ms = var.integration_timeout_ms
  })
}

resource "aws_apigatewayv2_api" "this" {
  name        = var.api_name
  description = var.api_description
  protocol_type = var.protocol_type
  body        = local.openapi_body
  tags        = var.tags
}

resource "aws_apigatewayv2_vpc_link" "this" {
  name               = "${var.api_name}-vpc-link"
  subnet_ids         = var.vpc_link_subnet_ids
  security_group_ids = var.vpc_link_security_group_ids
  tags               = var.tags
}

resource "aws_apigatewayv2_stage" "this" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = var.stage_name
  auto_deploy = true
  tags        = var.tags

  dynamic "access_log_settings" {
    for_each = var.enable_access_logs ? [1] : []
    content {
      destination_arn = aws_cloudwatch_log_group.api_gateway[0].arn
      format          = var.access_log_format
    }
  }
}

resource "aws_apigatewayv2_api_mapping" "this" {
  count       = var.custom_domain_name == null ? 0 : 1
  api_id      = aws_apigatewayv2_api.this.id
  domain_name = var.custom_domain_name
  stage       = aws_apigatewayv2_stage.this.id
}
