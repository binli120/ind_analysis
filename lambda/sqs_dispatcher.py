# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

import json
import os
from typing import Any, Dict, List

import boto3

ecs = boto3.client("ecs")
DB_ENABLED = os.getenv("DB_ENABLED", "false").lower() == "true"
CLUSTER = os.getenv("ECS_CLUSTER")
TASK_DEF = os.getenv("ECS_TASK_DEFINITION")
SUBNETS = os.getenv("ECS_SUBNETS", "").split(",")
SEC_GROUPS = os.getenv("ECS_SECURITY_GROUPS", "").split(",")


def handler(event: Dict[str, Any], _ctx=None) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for record in event.get("Records", []):
        payload = json.loads(record["body"])
        # Optionally touch DB ingestion_status here if you need
        run = ecs.run_task(
            cluster=CLUSTER,
            taskDefinition=TASK_DEF,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": SUBNETS,
                    "securityGroups": SEC_GROUPS,
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "pdf-analysis",
                        "command": [
                            "python",
                            "-m",
                            "scripts.pipeline_runner",  # your entrypoint
                            json.dumps(payload),
                        ],
                    }
                ]
            },
        )
        results.append({"status": "started", "taskArn": run["tasks"][0]["taskArn"]})
    return {"status": "ok", "results": results}
