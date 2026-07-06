"""
shared — Shared utilities and infrastructure for MuhafizSRE.

This package contains cross-cutting concerns used by multiple components
of the MuhafizSRE platform:

    - mcp_server: Model Context Protocol (MCP) telemetry server that exposes
      cloud logging traces, GitHub deployment history, and system metrics
      as tools consumable by AI agents performing SRE root-cause analysis.

Architecture Note:
    The ``shared`` package is intentionally kept dependency-light so that
    both the orchestrator agent and individual sub-agents can import from
    it without pulling in heavy framework code.  Only stdlib + ``mcp``
    SDK should appear in the dependency chain.
"""

# Re-export the mcp_server sub-package for convenient access:
#   from shared import mcp_server
#   mcp_instance = mcp_server.mcp
from . import mcp_server  # noqa: F401

__all__ = ["mcp_server"]
