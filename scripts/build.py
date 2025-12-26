# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

"""Simple build helper that installs deps and runs type/tests via Poetry."""

# author: Bin Lee
# email: blee@filynai.com

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def run(cmd: Sequence[str], *, cwd: Path = ROOT) -> None:
    """Execute a subprocess while logging the command."""
    logging.debug("Running: %s", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)


def main(argv: Sequence[str]) -> int:
    """Orchestrate the local CI flow (install, type-check, tests)."""
    parser = argparse.ArgumentParser(
        prog="build",
        description="Install dependencies, run formatting checks if available, and execute pytest.",
    )
    parser.add_argument(
        "--with",
        dest="with_groups",
        default="",
        help="Comma-separated poetry extras/groups to include (e.g. infra,llm).",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip dependency installation step.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip pytest execution.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper())

    extras = [group.strip() for group in args.with_groups.split(",") if group.strip()]
    extras_flag: list[str] = []
    if extras:
        extras_flag = ["--with", ",".join(extras)]

    if not args.skip_install:
        run(["poetry", "install", *extras_flag])

    # Optional static checks if configured in pyproject (best effort)
    try:
        run(["poetry", "run", "python", "-m", "mypy", "src"], cwd=ROOT)
    except subprocess.CalledProcessError:
        logging.warning("mypy failed; continuing to tests")

    if not args.skip_tests:
        run(["poetry", "run", "pytest"], cwd=ROOT)

    logging.info("Build pipeline finished successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
