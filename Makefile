PYTHON ?= python
POETRY ?= poetry
BUILD_DIR ?= build

S3_TO_SQS_SRC := lambda/s3_to_sqs.py
SQS_WORKER_SRC := lambda/sqs_worker.py

S3_TO_SQS_ZIP := $(BUILD_DIR)/s3_to_sqs.zip
SQS_WORKER_ZIP := $(BUILD_DIR)/sqs_worker.zip

.PHONY: help
help:
	@echo "Targets:"
	@echo "  lint             - run ruff/flake8/mypy if configured"
	@echo "  test             - run pytest"
	@echo "  package-lambdas  - build ZIPs for s3_to_sqs and sqs_worker into $(BUILD_DIR)"

.PHONY: lint
lint:
	$(POETRY) run ruff check || true
	$(POETRY) run flake8 || true
	$(POETRY) run mypy || true

.PHONY: test
test:
	$(POETRY) run pytest

.PHONY: dev-watch
dev-watch:
	@if ! command -v entr >/dev/null 2>&1; then \
		echo "entr is required (brew install entr or apt-get install entr)"; exit 1; \
	fi
	./scripts/dev_watch.sh

.PHONY: package-lambdas
package-lambdas: $(S3_TO_SQS_ZIP) $(SQS_WORKER_ZIP)
	@echo "Built lambda packages in $(BUILD_DIR)"

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(S3_TO_SQS_ZIP): $(S3_TO_SQS_SRC) | $(BUILD_DIR)
	cd lambda && zip -j ../$(S3_TO_SQS_ZIP) s3_to_sqs.py

$(SQS_WORKER_ZIP): $(SQS_WORKER_SRC) | $(BUILD_DIR)
	cd lambda && zip -j ../$(SQS_WORKER_ZIP) sqs_worker.py
