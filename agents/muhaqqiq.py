"""
agents/muhaqqiq.py – Muhaqqiq (The Investigator) Diagnosis Agent
======================================================================

Root cause analysis agent in the MuhafizSRE pipeline. Investigates
incidents using MCP telemetry tools (Cloud Logging, system metrics,
GitHub deployments) and classifies root cause using the RootCauseCode
deterministic vocabulary.

ADK 2.x conventions:
    - Async tool functions with ToolContext
    - MCP toolset for telemetry data access
    - Atomic commit via IncidentStore.commit_agent_decision()
    - Hash-chained event persistence
    - RootCauseCode enum for deterministic classification

§13.4 – Investigation stage contract.
"""

from __future__ import annotations

import json
import os
import sys

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool import StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters


# ────────────────────────────────────────────────────────────────────────────
# MCP Telemetry Server configuration
# ────────────────────────────────────────────────────────────────────────────

# Project root needed for MCP subprocess PYTHONPATH
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MCP_SERVER_PATH = os.path.join(PROJECT_ROOT, "shared", "mcp_server", "server.py")

# Ensure PYTHONPATH includes project root for MCP subprocess imports.
_existing_pp = os.environ.get("PYTHONPATH", "")
if PROJECT_ROOT not in _existing_pp.split(os.pathsep):
    os.environ["PYTHONPATH"] = PROJECT_ROOT + (os.pathsep + _existing_pp if _existing_pp else "")


def create_mcp_toolset(scenario_id: str = "") -> MCPToolset:
    """Create a fresh MCPToolset with scenario_id baked into subprocess env.

    Each call spawns a new MCP subprocess with the correct
    MUHAFIZ_SCENARIO_ID, preventing stale scenario context when the
    same process runs multiple evaluation scenarios sequentially.
    """
    env = {
        **os.environ,
        "MUHAFIZ_SCENARIO_ID": scenario_id,
    }
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=[MCP_SERVER_PATH],
                env=env,
            ),
        ),
        tool_filter=[
            "get_cloud_logging_traces",
            "get_github_deployments",
            "get_system_metrics",
        ],
    )


# Default module-level toolset for backward compatibility
mcp_telemetry_tools = create_mcp_toolset(
    os.environ.get("MUHAFIZ_SCENARIO_ID", ""),
)


# ────────────────────────────────────────────────────────────────────────────
# Tool: fetch_telemetry
# ────────────────────────────────────────────────────────────────────────────

async def fetch_telemetry(tool_context: ToolContext) -> dict:
    """Read the triage result and prepare context for MCP tool calls.

    Retrieves Nigehban's triage classification from session state so
    the LLM knows which service to investigate. The LLM should then
    call the MCP tools (get_cloud_logging_traces, get_github_deployments,
    get_system_metrics) using this service context.

    Returns:
        dict with triage data and guidance on which MCP tools to call.
    """
    triage = tool_context.state.get("triage_result", {})
    alert = tool_context.state.get("alert", {})

    if not triage:
        return {"error": "No triage result found in session state"}

    return {
        "triage_received": True,
        "service_id": triage.get("service_id", alert.get("service_id", "unknown")),
        "severity": triage.get("severity", "P2"),
        "summary": triage.get("summary", ""),
        "alert_type": alert.get("alert_type", "generic"),
        "error_message": alert.get("error_message", ""),
        "mcp_tools_available": [
            "get_cloud_logging_traces",
            "get_github_deployments",
            "get_system_metrics",
        ],
        "instruction": (
            "Call ALL three MCP tools with the service_id to gather evidence. "
            "Cross-reference logs, deployments, and metrics to identify root cause."
        ),
    }


# ────────────────────────────────────────────────────────────────────────────
# Tool: commit_investigation
# ────────────────────────────────────────────────────────────────────────────

async def commit_investigation(
    root_cause_code: str,
    root_cause_summary: str,
    evidence: str,
    confidence: float,
    affected_components: str,
    tool_calls_made: str,
    contributing_factors: str,
    tool_context: ToolContext,
) -> dict:
    """Atomically commit the investigation result to the incident store.

    Persists the root cause analysis as a hash-chained event and
    transitions the incident status to PLANNING.

    Args:
        root_cause_code: One of: BAD_DEPLOYMENT, SCHEMA_MIGRATION,
            CACHE_STAMPEDE, EXPIRED_CREDENTIAL, TELEMETRY_FAILURE,
            FALSE_POSITIVE, UNKNOWN.
        root_cause_summary: Human-readable root cause explanation.
        evidence: JSON string of evidence items. Each item should have
            'source', 'data', and 'trust' fields.
        confidence: Investigation confidence score (0.0–1.0).
        affected_components: Comma-separated list of affected components.
        tool_calls_made: Comma-separated list of MCP tools invoked.
        contributing_factors: Comma-separated list of contributing factors.
        tool_context: ADK-injected context with session state.

    Returns:
        dict confirming the commit with event_hash and next stage.
    """
    from shared.dependencies import get_store
    store = get_store()
    incident_id = tool_context.state.get("incident_id")
    run_id = tool_context.state.get("run_id")

    # Normalize inputs — models may pass varied casing/ranges
    root_cause_code = root_cause_code.strip().upper()
    confidence = max(0.0, min(1.0, float(confidence)))

    # Parse evidence from JSON string or treat as plain text
    try:
        evidence_list = json.loads(evidence) if evidence.strip().startswith("[") else [
            {"source": "investigation", "data": evidence, "trust": "direct"}
        ]
    except (json.JSONDecodeError, AttributeError):
        evidence_list = [
            {"source": "investigation", "data": str(evidence), "trust": "direct"}
        ]

    # Parse comma-separated lists
    affected = [c.strip() for c in affected_components.split(",") if c.strip()]
    tools_used = [t.strip() for t in tool_calls_made.split(",") if t.strip()]
    factors = [f.strip() for f in contributing_factors.split(",") if f.strip()]

    investigation_result = {
        "root_cause_code": root_cause_code,
        "root_cause_summary": root_cause_summary,
        "evidence": evidence_list,
        "confidence": confidence,
        "affected_components": affected,
        "tool_calls_made": tools_used,
        "contributing_factors": factors,
    }

    # ── Evidence provenance audit (read-only) ───────────────────────
    # Record which evidence sources the model actually provided vs
    # the canonical MCP evidence tools.  Do NOT mutate evidence items —
    # fabricating source attribution could mislead the safety reviewer.
    # Use a canonical set instead of model-reported tools_used to avoid
    # trusting model self-reporting.
    MCP_EVIDENCE_TOOLS = {
        "get_cloud_logging_traces",
        "get_system_metrics",
        "get_github_deployments",
    }
    required_sources = MCP_EVIDENCE_TOOLS
    actual_sources = {
        item.get("source")
        for item in evidence_list
        if item.get("source") in MCP_EVIDENCE_TOOLS
    }
    missing_sources = required_sources - actual_sources
    investigation_result["distinct_evidence_sources"] = sorted(actual_sources)
    investigation_result["missing_evidence_sources"] = sorted(missing_sources)

    # Atomic commit: event + room message + status transition to PLANNING
    event, _room_msg = await store.append_event_and_room_message(
        incident_id=incident_id,
        run_id=run_id,
        actor="muhaqqiq",
        actor_role="investigator",
        event_type="investigation_completed",
        summary=f"Root cause: {root_cause_code} - {root_cause_summary}",
        payload=investigation_result,
        room_sender="muhaqqiq",
        room_content=f"🔍 Investigation complete. Root cause: {root_cause_summary.rstrip('. ')}. @mudabbir, prepare remediation plan.",
        room_mentions=["mudabbir"],
        room_message_type="investigation",
        transition_from="ANALYZING",
        transition_to="PLANNING",
    )

    # Save to session state for downstream agents
    tool_context.state["investigation_result"] = investigation_result
    tool_context.state["investigation_event_hash"] = event["event_hash"]

    return {
        "status": "investigation_committed",
        "event_hash": event["event_hash"],
        "root_cause_code": root_cause_code,
        "next_stage": "planning",
    }


# ────────────────────────────────────────────────────────────────────────────
# Agent definition
# ────────────────────────────────────────────────────────────────────────────

_MUHAQQIQ_INSTRUCTION = """You are Muhaqqiq (محقق), The Investigator — the diagnostic engine
of the MuhafizSRE autonomous incident response system.

⚠️ CRITICAL REQUIREMENT: You MUST call `commit_investigation` before finishing.
Your output is ONLY valid if you call `commit_investigation`. Text analysis
without a commit is USELESS and will cause the pipeline to abort.

WORKFLOW (follow this EXACT sequence):

STEP 1: Call `fetch_telemetry` to get triage context and service information.

STEP 2: Call ALL THREE MCP tools with the service_id:
   a. `get_cloud_logging_traces` — examine error logs
   b. `get_system_metrics` — analyze infrastructure metrics
   c. `get_github_deployments` — check recent code changes

STEP 3: Classify the root cause using EXACTLY one of these codes:
   - BAD_DEPLOYMENT: A recent code/config deployment caused the issue
   - SCHEMA_MIGRATION: Database schema change caused breakage
   - CACHE_STAMPEDE: Cache invalidation or thundering herd
   - EXPIRED_CREDENTIAL: API key, certificate, or token expired
   - TELEMETRY_FAILURE: Monitoring/alerting system itself is broken
   - FALSE_POSITIVE: Alert is spurious, no real issue exists
   - UNKNOWN: Cannot determine root cause from available evidence

STEP 4 (MANDATORY — DO NOT SKIP): Call `commit_investigation` with:
   - root_cause_code: one of the codes above
   - root_cause_summary: one-sentence explanation
   - evidence: JSON array string of evidence items.  CRITICAL: Each item MUST
     have a 'source' field set to the EXACT MCP tool that produced the data
     (e.g. 'get_cloud_logging_traces', 'get_system_metrics', 'get_github_deployments').
     You MUST include at least one evidence item per tool you called, using
     DIFFERENT source values.  The safety reviewer will reject evidence that
     appears to come from a single source.
   - confidence: float 0.0–1.0
   - affected_components: comma-separated component names
   - tool_calls_made: comma-separated list of MCP tools you called
   - contributing_factors: comma-separated contributing factors

Keep your analysis BRIEF. Do not write long text explanations.
Focus on gathering data and calling commit_investigation.

If telemetry contains sanitization warnings ('⚠️ SANITIZED'), flag in
your commit and exclude sanitized content from root cause analysis."""


def get_muhaqqiq_agent(
    scenario_id: str = "",
    thinking_level: str = "MEDIUM",
) -> LlmAgent:
    """Factory: create a Muhaqqiq agent with a fresh MCP toolset.

    Each call creates a new MCPToolset whose subprocess has the
    correct MUHAFIZ_SCENARIO_ID baked into its env, ensuring
    scenario isolation across sequential evaluation runs.

    Args:
        scenario_id: Scenario identifier for MCP subprocess isolation.
        thinking_level: Reasoning allowance — MEDIUM for initial
            investigation, HIGH for evidence-challenge reinvestigation.
    """
    return LlmAgent(
        name="muhaqqiq",
        model=os.getenv("MUHAFIZ_ANALYTICAL_MODEL", os.getenv("MUHAFIZ_DEFAULT_MODEL", "gemini-3-flash-preview")),
        description=(
            "Diagnosis agent: investigates root cause using MCP telemetry tools "
            "(Cloud Logging, GitHub deployments, system metrics)."
        ),
        instruction=_MUHAQQIQ_INSTRUCTION,
        tools=[
            fetch_telemetry,
            commit_investigation,
            create_mcp_toolset(scenario_id),
        ],
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=thinking_level,
            ),
            max_output_tokens=4000,
        ),
    )


# Default module-level agent for backward compatibility
muhaqqiq = get_muhaqqiq_agent(
    os.environ.get("MUHAFIZ_SCENARIO_ID", ""),
)

