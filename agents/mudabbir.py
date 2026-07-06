"""
agents/mudabbir.py – Mudabbir (The Commander) Remediation Planning Agent
==============================================================================

Strategic remediation planner in the MuhafizSRE pipeline. Creates
bounded, typed, validated mitigation plans using ONLY the allowlisted
skills and services defined in the action policy.

ADK 2.x conventions:
    - Async tool functions with ToolContext
    - ActionEnvelope-based plan structure
    - validate_action_graph for acyclic, typed, bounded action validation
    - Atomic commit via IncidentStore.commit_agent_decision()
    - Hash-chained event persistence

§13.5 – Planning stage contract.
§16   – Bounded action envelopes.
"""

from __future__ import annotations

import json
import os
import uuid

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext
from google.genai import types

from gateway.models import (
    ActionEnvelope,
    MitigationPlan,
    sha256_hex,
)
from shared.action_policy import (
    ALLOWED_SKILLS,
    ALLOWED_TARGETS,
    validate_action_graph,
)


# ────────────────────────────────────────────────────────────────────────────
# Tool: commit_plan
# ────────────────────────────────────────────────────────────────────────────

async def commit_plan(
    strategy_summary: str,
    risk_level: str,
    estimated_mttr_minutes: int,
    actions_json: str,
    tool_context: ToolContext,
) -> dict:
    """Create a MitigationPlan with validated ActionEnvelopes and commit atomically.

    Validates all actions against ALLOWED_SKILLS and SKILL_ARGUMENT_SCHEMAS
    via validate_action_graph(), then persists the plan as a hash-chained
    event and transitions incident status to REVIEWING.

    Args:
        strategy_summary: High-level description of the remediation strategy.
        risk_level: Risk classification — one of: critical, high, medium, low.
        estimated_mttr_minutes: Estimated Mean Time To Repair in minutes.
        actions_json: JSON array of action objects. Each action must have:
            - action_id (str): Unique identifier within this plan
            - skill (str): One of the allowed skills
            - target (str): One of the allowed services
            - arguments (dict): Typed arguments matching the skill schema
            - depends_on (list[str], optional): Action IDs this depends on
            - on_failure (str, optional): "STOP" or "CONTINUE" (default STOP)
        tool_context: ADK-injected context with session state.

    Returns:
        dict confirming the commit, or dict with validation errors.
    """
    from shared.dependencies import get_store
    store = get_store()
    incident_id = tool_context.state.get("incident_id")
    run_id = tool_context.state.get("run_id")

    # Normalize risk_level — models may pass "HIGH", "Medium", etc.
    risk_level = risk_level.strip().lower()

    # ── Parse actions from JSON ──────────────────────────────────────────
    try:
        raw_actions = json.loads(actions_json)
        if not isinstance(raw_actions, list):
            return {"status": "error", "errors": ["actions_json must be a JSON array"]}
    except json.JSONDecodeError as exc:
        return {"status": "error", "errors": [f"Invalid JSON in actions_json: {exc}"]}

    # ── Build typed ActionEnvelope models ────────────────────────────────
    envelopes: list[ActionEnvelope] = []
    parse_errors: list[str] = []
    for i, raw in enumerate(raw_actions):
        try:
            envelope = ActionEnvelope(
                action_id=raw.get("action_id", f"action-{i+1}"),
                skill=raw.get("skill", ""),
                target=raw.get("target", ""),
                arguments=raw.get("arguments", {}),
                depends_on=raw.get("depends_on", []),
                on_failure=raw.get("on_failure", "STOP"),
            )
            envelopes.append(envelope)
        except Exception as exc:
            parse_errors.append(f"Action {i}: {exc}")

    if parse_errors:
        return {"status": "error", "errors": parse_errors}

    # ── Validate the action graph ────────────────────────────────────────
    is_valid, validation_errors = validate_action_graph(envelopes)
    if not is_valid:
        return {
            "status": "validation_failed",
            "errors": validation_errors,
            "hint": (
                f"Allowed skills: {sorted(ALLOWED_SKILLS)}. "
                f"Allowed targets: {sorted(ALLOWED_TARGETS)}. "
                "Check argument schemas match the skill requirements."
            ),
        }

    # ── Build the MitigationPlan ─────────────────────────────────────────
    revision = tool_context.state.get("plan_revision", 1)
    feedback = tool_context.state.get("revision_feedback", "") or tool_context.state.get("challenge_feedback", "")

    plan = MitigationPlan(
        plan_id=f"PLAN-{uuid.uuid4().hex[:8].upper()}",
        revision=revision,
        actions=envelopes,
        strategy_summary=strategy_summary + (f" [Revision feedback: {feedback}]" if feedback else ""),
        risk_level=risk_level,
        estimated_mttr_minutes=estimated_mttr_minutes,
    )

    plan_dict = plan.model_dump(mode="json")
    plan_hash = sha256_hex(plan_dict)

    # ── Atomic commit: event + room message + status transition to REVIEWING ─
    action_count = len(envelopes)
    event, _room_msg = await store.append_event_and_room_message(
        incident_id=incident_id,
        run_id=run_id,
        actor="mudabbir",
        actor_role="commander",
        event_type="plan_created",
        summary=f"Plan {plan.plan_id}: {strategy_summary}",
        payload={
            "plan": plan_dict,
            "plan_hash": plan_hash,
            "action_count": action_count,
        },
        room_sender="mudabbir",
        room_content=f"📐 Plan ready (Rev {revision}): {action_count} actions. Risk: {risk_level}. @muhtasib, review for safety.",
        room_mentions=["muhtasib"],
        room_message_type="plan",
        transition_from="PLANNING",
        transition_to="REVIEWING",
    )

    # Save to session state for downstream agents
    tool_context.state["plan"] = plan_dict
    tool_context.state["plan_hash"] = plan_hash
    tool_context.state["plan_event_hash"] = event["event_hash"]

    return {
        "status": "plan_committed",
        "plan_id": plan.plan_id,
        "plan_hash": plan_hash,
        "event_hash": event["event_hash"],
        "action_count": action_count,
        "next_stage": "safety_review",
    }


# ────────────────────────────────────────────────────────────────────────────
# Agent factory
# ────────────────────────────────────────────────────────────────────────────

def get_mudabbir_agent(thinking_level: str = "MEDIUM") -> LlmAgent:
    """Factory: create a Mudabbir agent with the specified thinking level.

    Args:
        thinking_level: Reasoning allowance — MEDIUM for initial planning,
            HIGH for plan revision after safety challenge.
    """
    return LlmAgent(
        name="mudabbir",
        model=os.getenv("MUHAFIZ_ANALYTICAL_MODEL", os.getenv("MUHAFIZ_DEFAULT_MODEL", "gemini-3-flash-preview")),
        description=(
            "Commander agent: creates bounded, validated remediation plans "
            "using allowlisted skills and services."
        ),
        instruction="""You are Mudabbir (مدبر), The Commander — the tactical strategist
of the MuhafizSRE autonomous incident response system.

⚠️ CRITICAL REQUIREMENT: You MUST call `commit_plan` before finishing.
Your output is ONLY valid if you call `commit_plan`. Text analysis
without a commit is USELESS and will cause the pipeline to abort.

WORKFLOW (follow this EXACT sequence):

STEP 1: Read the investigation result and triage result from the context provided.

STEP 2: Select the minimum set of remediation actions needed.

STEP 3 (MANDATORY — DO NOT SKIP): Call `commit_plan` with:
   - strategy_summary: one-sentence remediation strategy
   - risk_level: low / medium / high / critical
   - estimated_mttr_minutes: integer estimate
   - actions_json: JSON array string of action objects

ALLOWED SKILLS (use ONLY these exact skill names):
  - rollback_service_revision: {"service_name": "<service>", "target_revision": "<rev>"}
  - apply_rate_limit: {"service_name": "<service>", "requests_per_second": <1-1000>, "duration_seconds": <30-900>}
  - scale_service: {"service_name": "<service>", "replicas": <1-6>}
  - flush_cache: {"service_name": "<service>", "cache_type": "<type>"}
  - rotate_credentials: {"service_name": "<service>", "credential_type": "<api_key|db_password|jwt_signing_key|service_account|tls_cert>"}
  - restart_service: {"service_name": "<service>", "graceful": true/false}

ALLOWED TARGET SERVICES: auth-service, payment-gateway, user-service

EXAMPLE actions_json (pass as a string):
[
  {"action_id": "step-1", "skill": "rollback_service_revision", "target": "auth-service",
   "arguments": {"service_name": "auth-service", "target_revision": "v2.3.1"},
   "depends_on": [], "on_failure": "STOP"},
  {"action_id": "step-2", "skill": "flush_cache", "target": "auth-service",
   "arguments": {"service_name": "auth-service", "cache_type": "all"},
   "depends_on": ["step-1"], "on_failure": "CONTINUE"}
]

SKILL SELECTION GUIDANCE by root cause:
  - BAD_DEPLOYMENT → rollback_service_revision + flush_cache
  - CACHE_STAMPEDE → flush_cache + scale_service (clear stale cache, then add capacity)
  - EXPIRED_CREDENTIAL → rotate_credentials + restart_service
  - If Redis/cache is the root cause, ALWAYS include flush_cache.

Keep your analysis VERY BRIEF. Focus on calling commit_plan.
Be pragmatic. Choose the LEAST disruptive fix. Prefer rollbacks over restarts.""",
        tools=[commit_plan],
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=thinking_level,
            ),
            max_output_tokens=4000,
        ),
    )


# Default module-level agent for backward compatibility
mudabbir = get_mudabbir_agent()
