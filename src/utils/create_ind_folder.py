# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""
@author: Bin Lee
@email: blee@filynai.com

Utility script to scaffold IND project folders in S3 from a template.
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Protocol

try:
    import boto3
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    boto3 = None  # type: ignore[assignment]

DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "s3_folder_templates.json"
)


class S3ClientProtocol(Protocol):
    """Minimal protocol for the boto3 S3 client used here."""

    def put_object(self, *, Bucket: str, Key: str, **kwargs: Any) -> Dict[str, Any]: ...


def load_template(file_path: Path) -> Dict[str, Any]:
    """Load folder template JSON."""
    template_path = Path(file_path)
    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")
    with template_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def create_structure(
    s3_client: S3ClientProtocol,
    bucket: str,
    base_prefix: str,
    structure: Dict[str, Any],
) -> None:
    """Recursively create folders in S3 based on the JSON structure."""
    for folder_name, subfolders in structure.items():
        prefix = f"{base_prefix}{folder_name}/"
        s3_client.put_object(Bucket=bucket, Key=prefix)
        print(f" Created: s3://{bucket}/{prefix}")
        if isinstance(subfolders, dict) and subfolders:
            create_structure(s3_client, bucket, prefix, subfolders)

# @author: Bin Lee
# @email: blee@filynai.com


def create_new_project(
    bucket: str, root_prefix: str, project_name: str, template_file: Path
) -> None:
    """Create a new project folder in S3 using the template."""
    if boto3 is None:
        raise RuntimeError("boto3 is required to create project folders in S3.")
    s3 = boto3.client("s3")
    structure = load_template(template_file)
    base_prefix = f"{root_prefix.rstrip('/')}/{project_name}/"
    print(f"\n Creating IND folder structure for project: {project_name}")
    create_structure(s3, bucket, base_prefix, structure)
    print(f"\n Completed: s3://{bucket}/{base_prefix}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create IND-style folder structure in S3 using a JSON template."
    )
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument(
        "--prefix", default="IND_Projects", help="Root folder prefix in S3"
    )
    parser.add_argument(
        "--project", required=True, help="New project name (e.g., Project_001)"
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE_PATH,
        help="Path to folder template JSON",
    )

    args = parser.parse_args()

    create_new_project(args.bucket, args.prefix, args.project, args.template)
