"""
S3 → SQS bridge.

Triggered by S3 ObjectCreated events. Pushes a minimal payload to SQS so the
worker Lambda can download, process, and persist results.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import boto3

sqs = boto3.client("sqs")
QUEUE_URL = os.environ.get("PIPELINE_QUEUE_URL")
DEFAULT_TENANT_ID = os.environ.get("DEFAULT_TENANT_ID")
DEFAULT_USER_ID = os.environ.get("DEFAULT_USER_ID")


def handler(event: Dict[str, Any], _ctx=None) -> Dict[str, Any]:
    if not QUEUE_URL:
        raise RuntimeError("PIPELINE_QUEUE_URL env var is required")
    records = event.get("Records") or []
    responses = []
    for record in records:
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        obj = s3_info.get("object", {}) or {}
        key = obj.get("key")
        version_id = obj.get("versionId")
        if not bucket or not key:
            responses.append({"status": "ignored", "reason": "missing bucket/key"})
            continue
        payload = {
            "bucket": bucket,
            "key": key,
            "version_id": version_id,
            "tenant_id": DEFAULT_TENANT_ID,
            "created_by": DEFAULT_USER_ID,
        }
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(payload))
        responses.append({"status": "enqueued", "payload": payload})
    return {"status": "ok", "responses": responses}
