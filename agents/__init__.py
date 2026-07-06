"""agents/__init__.py – MuhafizSRE Agent Package

Exports the 5 specialized ADK agents and the AGENT_REGISTRY.

Agent Fleet:
    1. Nigehban  (نگہبان)  — The Watchman     — Triage & severity classification
    2. Muhaqqiq  (محقق)    — The Investigator  — Root cause diagnosis via MCP
    3. Mudabbir  (مدبر)    — The Commander     — Bounded remediation planning
    4. Muhtasib  (محتسب)   — The Inspector     — Adversarial safety review
    5. Aamil     (عامل)    — The Executor      — Contract-based execution

Architecture:
    gateway-orchestrated, explicitly routed ADK multi-agent workflow.
    The gateway explicitly routes alerts through agents in order,
    supporting false alarm short-circuit and challenge routing.
"""

from __future__ import annotations

# Agent registry for gateway routing
from .agent import AGENT_REGISTRY  # noqa: F401

# Individual agent exports for direct access
from .nigehban import nigehban    # noqa: F401
from .muhaqqiq import muhaqqiq    # noqa: F401
from .mudabbir import mudabbir    # noqa: F401
from .muhtasib import muhtasib    # noqa: F401
from .aamil import aamil          # noqa: F401

__all__ = [
    "AGENT_REGISTRY",
    "nigehban",
    "muhaqqiq",
    "mudabbir",
    "muhtasib",
    "aamil",
]
