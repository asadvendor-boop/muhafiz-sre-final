"""
shared.mcp_server — MuhafizSRE Model Context Protocol (MCP) Telemetry Server.

This sub-package spins up a FastMCP server exposing three SRE telemetry
tools that AI agents call during incident investigation:

    1. **get_cloud_logging_traces** — Google Cloud Logging error stacks
    2. **get_github_deployments**   — Recent PR merges / deployment history
    3. **get_system_metrics**       — CPU / Memory / Latency time-series

Usage (programmatic)::

    from shared.mcp_server import mcp
    mcp.run()                       # stdio transport by default

Usage (CLI)::

    python -m shared.mcp_server.server

The ``mcp`` object is the single FastMCP instance shared across the
entire process — import it wherever you need to register additional
tools or resources.
"""

# ── Public API ──────────────────────────────────────────────────────────
# Import the FastMCP instance *and* the module so that tool registrations
# (which happen at module-import time via decorators) are executed.
from .server import mcp  # noqa: F401

__all__ = ["mcp"]
