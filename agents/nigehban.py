"""
agents/nigehban.py – Nigehban (The Watchman) Triage Agent
================================================================

First responder in the MuhafizSRE incident pipeline. Rapidly classifies
incoming alerts by severity (P0–P4) and filters false alarms before
they consume diagnostic resources.

ADK 2.x conventions:
    - Async tool functions with ToolContext
    - Atomic commit via IncidentStore.commit_agent_decision()
    - Session state for inter-agent data flow
    - Hash-chained event persistence

§13.3 – Triage stage contract.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext
from google.genai import types


# ────────────────────────────────────────────────────────────────────────────
# Tool: consume_alert
# ────────────────────────────────────────────────────────────────────────────

async def consume_alert(tool_context: ToolContext) -> dict:
    """Read the injected alert from session state.

    The gateway injects the raw alert dict into session state before
    launching the pipeline. This tool retrieves it so the LLM can
    reason about severity and actionability.

    Returns:
        dict with alert fields: service_id, alert_type, error_message,
        severity, summary, timestamp.
    """
    alert = tool_context.state.get("alert", {})
    return {
        "alert_received": True,
        "service_id": alert.get("service_id", "unknown"),
        "alert_type": alert.get("alert_type", "generic"),
        "error_message": alert.get("error_message", ""),
        "severity": alert.get("severity", "P2"),
        "summary": alert.get("summary", ""),
        "timestamp": alert.get("timestamp", ""),
    }


# ────────────────────────────────────────────────────────────────────────────
# Tool: commit_triage
# ────────────────────────────────────────────────────────────────────────────

async def commit_triage(
    severity: str,
    service_id: str,
    summary: str,
    is_actionable: bool,
    confidence: float,
    tool_context: ToolContext,
) -> dict:
    """Atomically commit the triage result to the incident store.

    Persists the triage decision as a hash-chained event and updates
    incident status to ANALYZING (if actionable) or FALSE_ALARM
    (if not actionable).

    Args:
        severity: Assessed severity tier (P0–P4).
        service_id: Affected service identifier.
        summary: Concise triage summary.
        is_actionable: Whether the alert requires investigation.
        confidence: Triage confidence score (0.0–1.0).
        tool_context: ADK-injected context with session state.

    Returns:
        dict confirming the commit with event_hash and next stage.
    """
    from shared.dependencies import get_store
    store = get_store()
    incident_id = tool_context.state.get("incident_id")
    run_id = tool_context.state.get("run_id")

    triage_result = {
        "severity": severity,
        "service_id": service_id,
        "summary": summary,
        "is_actionable": is_actionable,
        "confidence": confidence,
    }

    # Determine status transition based on actionability
    if not is_actionable:
        transition_to = "FALSE_ALARM"
        room_content = f"🟢 Alert for {service_id} classified as FALSE ALARM. No action required."
        room_mentions: list[str] | None = None
    else:
        transition_to = "ANALYZING"
        root_cause = summary  # best available hypothesis at triage stage
        room_content = f"⚠️ {severity} incident confirmed on {service_id}. Root cause hypothesis: {root_cause.rstrip('. ')}. @muhaqqiq, investigate the deployment correlation."
        room_mentions = ["muhaqqiq"]

    # Atomic commit: event + room message + status transition
    event, _room_msg = await store.append_event_and_room_message(
        incident_id=incident_id,
        run_id=run_id,
        actor="nigehban",
        actor_role="triage",
        event_type="triage_completed",
        summary=f"Triage: {severity} - {summary}",
        payload=triage_result,
        room_sender="nigehban",
        room_content=room_content,
        room_mentions=room_mentions,
        room_message_type="triage",
        transition_from="DETECTED",
        transition_to=transition_to,
    )

    # Save to session state for downstream agents
    tool_context.state["triage_result"] = triage_result
    tool_context.state["triage_event_hash"] = event["event_hash"]

    return {
        "status": "triage_committed",
        "event_hash": event["event_hash"],
        "is_actionable": is_actionable,
        "next_stage": "investigation" if is_actionable else "closed",
    }


# ────────────────────────────────────────────────────────────────────────────
# Agent factory
# ────────────────────────────────────────────────────────────────────────────

def get_nigehban_agent(thinking_level: str = "LOW") -> LlmAgent:
    """Factory: create a Nigehban agent with the specified thinking level.

    Args:
        thinking_level: Reasoning allowance — LOW for normal triage.
    """
    return LlmAgent(
        name="nigehban",
        model=os.getenv("MUHAFIZ_SPEED_MODEL", os.getenv("MUHAFIZ_DEFAULT_MODEL", "gemini-3.1-flash-lite")),
        description="Triage agent: classifies incident severity, filters false alarms.",
        instruction="""You are Nigehban (نگہبان), The Watchman — first responder in MuhafizSRE.

Your mission: rapidly triage incoming alerts.

1. Call `consume_alert` to retrieve the raw alert data.
2. Analyze the alert:
   - P0 (Critical): Complete outage, data loss, security breach
   - P1 (High): Major degradation, >50% error rate
   - P2 (Medium): Partial degradation, elevated latency
   - P3 (Low): Minor issues, non-critical
   - P4 (Informational): Noise, no action needed
3. Determine if actionable (true) or false alarm (false).
4. Call `commit_triage` with your classification.
   - confidence: 0.0 to 1.0 how sure you are
   - For P4 with is_actionable=false, this auto-closes as FALSE_ALARM

Be decisive. Speed > exhaustive analysis at triage stage.
When uncertain, err on higher severity.""",
        tools=[consume_alert, commit_triage],
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=thinking_level,
            ),
            max_output_tokens=500,
        ),
    )


# Default module-level agent for backward compatibility
nigehban = get_nigehban_agent()

