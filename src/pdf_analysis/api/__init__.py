# Copyright (c) 2025 longooc.com
# Author: Bin Lee
# Email: blee@longooc.com

# @author: Bin Lee
# @email: blee@longooc.com

"""Expose the FastAPI application instance for external servers/tooling."""

from .server import app

__all__ = ["app"]
