terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  stage_topics = {
    upload_ingestion = {
      input       = "${var.name_prefix}-upload-ingestion"
      completion  = "${var.name_prefix}-upload-ingestion-completed"
      description = "Upload ingestion notifications and completion events."
    }
    pdf_parsing_chunking = {
      input       = "${var.name_prefix}-pdf-parsing-chunking"
      completion  = "${var.name_prefix}-pdf-parsing-chunking-completed"
      description = "PDF parsing and chunking workflow notifications."
    }
    classification_template_matching = {
      input       = "${var.name_prefix}-classification-template-matching"
      completion  = "${var.name_prefix}-classification-template-matching-completed"
      description = "Classification and template matching stage notifications."
    }
    metadata_summary_extraction = {
      input       = "${var.name_prefix}-metadata-summary-extraction"
      completion  = "${var.name_prefix}-metadata-summary-extraction-completed"
      description = "Metadata and summary extraction stage notifications."
    }
    embedding_indexing = {
      input       = "${var.name_prefix}-embedding-indexing"
      completion  = "${var.name_prefix}-embedding-indexing-completed"
      description = "Embedding generation and indexing events."
    }
    reranker = {
      input       = "${var.name_prefix}-reranker"
      completion  = "${var.name_prefix}-reranker-completed"
      description = "Reranker stage notifications."
    }
    module26_narrative_writers = {
      input       = "${var.name_prefix}-module26-narrative-writers"
      completion  = "${var.name_prefix}-module26-narrative-writers-completed"
      description = "Module 2.6 narrative drafting notifications."
    }
    module26_tabulators = {
      input       = "${var.name_prefix}-module26-tabulators"
      completion  = "${var.name_prefix}-module26-tabulators-completed"
      description = "Module 2.6 table generation events."
    }
    module24_synthesizer = {
      input       = "${var.name_prefix}-module24-synthesizer"
      completion  = "${var.name_prefix}-module24-synthesizer-completed"
      description = "Module 2.4 synthesis stage notifications."
    }
    validation_scoring = {
      input       = "${var.name_prefix}-validation-scoring"
      completion  = "${var.name_prefix}-validation-scoring-completed"
      description = "Validation and scoring events."
    }
    packaging_submission = {
      input       = "${var.name_prefix}-packaging-submission"
      completion  = "${var.name_prefix}-packaging-submission-completed"
      description = "Packaging and submission notifications."
    }
    pdf_extraction = {
      input       = "${var.name_prefix}-pdf-extraction"
      completion  = "${var.name_prefix}-pdf-extraction-completed"
      description = "Low-level PDF extraction stage notifications."
    }
    zeroshot_labeling = {
      input       = "${var.name_prefix}-zeroshot-labeling"
      completion  = "${var.name_prefix}-zeroshot-labeling-completed"
      description = "Zero-shot labeling events."
    }
  }

  stage_successors = {
    "upload-ingestion"                 = ["pdf-parsing-chunking"]
    "pdf-parsing-chunking"             = ["classification-template-matching"]
    "classification-template-matching" = ["metadata-summary-extraction"]
    "metadata-summary-extraction"      = ["embedding-indexing"]
    "embedding-indexing"               = ["reranker"]
    "reranker"                         = ["module26-narrative-writers", "module26-tabulators"]
    "module26-narrative-writers"       = ["module24-synthesizer"]
    "module26-tabulators"              = ["module24-synthesizer"]
    "module24-synthesizer"             = ["validation-scoring"]
    "validation-scoring"               = ["packaging-submission"]
    "packaging-submission"             = []
  }

  stage_successor_arns = {
    for stage, successors in local.stage_successors :
    stage => [
      for successor in successors :
      aws_sns_topic.stage_input[replace(successor, "-", "_")].arn
    ]
  }
}

resource "aws_sns_topic" "stage_input" {
  for_each = local.stage_topics

  name         = each.value.input
  display_name = title(replace(each.key, "_", " "))

  tags = merge(var.tags, {
    Stage       = each.key
    TopicType   = "input"
    Environment = var.environment
  })
}

resource "aws_sns_topic" "stage_completion" {
  for_each = local.stage_topics

  name         = each.value.completion
  display_name = "${title(replace(each.key, "_", " "))} completion"

  tags = merge(var.tags, {
    Stage       = each.key
    TopicType   = "completion"
    Environment = var.environment
  })
}
