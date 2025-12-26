# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

# @author: Bin Lee
# @email: blee@filynai.com

"""Expose the FastAPI application instance for external servers/tooling."""

from .server import app

__all__ = ["app"]
