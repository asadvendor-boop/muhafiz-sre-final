"""
agents/aamil.py – Aamil (The Executor) Execution Agent
=============================================================

Disciplined executor in the MuhafizSRE pipeline. Executes ONLY from
approved immutable contracts. Runs actions in topological order via
skill adapters, records receipts, and writes results to session state.
The gateway handles recovery verification and finalization.

ADK 2.x conventions:
    - Async tool functions with ToolContext
    - Contract-based execution
    - Topological action ordering via action_policy.topological_sort
    - Hash-chained event persistence

§20 – Execution stage contract.
"""

from __future__ import annotations

import logging
import os

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext
from google.genai import types

from gateway.models import (
    ActionEnvelope,
)
from shared.action_policy import (
    check_action_eligibility,
    topological_sort,
    validate_single_action,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Skill adapters — simulated execution
# ────────────────────────────────────────────────────────────────────────────

from shared.skills import execute_skill as _execute_skill_async  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Tool: execute_approved_actions
# ────────────────────────────────────────────────────────────────────────────

async def execute_approved_actions(tool_context: ToolContext) -> dict:
    """Execute actions from the gateway-validated execution snapshot.

    The gateway has already:
      1. Validated the contract via claim_execution_snapshot()
      2. Injected the immutable snapshot into session state
      3. Transitioned contract/incident to EXECUTING

    This tool reads actions EXCLUSIVELY from the snapshot — it NEVER
    fetches the contract from the database independently. The gateway
    is the sole authority boundary for execution authorization.
    """
    incident_id = tool_context.state.get("incident_id")
    run_id = tool_context.state.get("run_id")

    # ── Read from gateway-validated snapshot ONLY ────────────────────────
    # This check MUST happen before any store access. If the snapshot is
    # missing, it means the gateway did not validate — authority violation.
    snapshot = tool_context.state.get("execution_snapshot")
    if not snapshot:
        return {
            "status": "error",
            "message": (
                "No execution_snapshot in session state. "
                "The gateway must validate and inject the snapshot before "
                "invoking Aamil. This is an authority boundary violation."
            ),
        }

    contract_id = snapshot["contract_id"]
    revision = snapshot["revision"]

    # Store access only after snapshot validation passes
    from shared.dependencies import get_store
    store = get_store()

    incident = await store.get_incident(incident_id)
    scenario_id = incident.get("scenario_id") if incident else None

    # ── Parse actions from the snapshot (canonical source) ───────────────
    try:
        actions_data = snapshot.get("actions", [])
        envelopes = [ActionEnvelope(**a) for a in actions_data]
    except Exception as exc:
        logger.error("Failed to parse snapshot actions: %s", exc)
        return {
            "status": "error",
            "message": f"Failed to parse actions from execution snapshot: {exc}",
        }

    if not envelopes:
        return {
            "status": "error",
            "message": "No actions found in contract or plan.",
        }

    # ── Execute in topological order ─────────────────────────────────────
    try:
        sorted_actions = topological_sort(envelopes)
    except ValueError as exc:
        return {"status": "error", "message": f"Action graph error: {exc}"}

    receipts: dict[str, dict] = {}
    all_succeeded = True

    for action in sorted_actions:
        eligible, reason = check_action_eligibility(
            action, receipts, sorted_actions,
        )
        if not eligible:
            receipts[action.action_id] = {
                "status": "skipped",
                "reason": reason,
                "action_id": action.action_id,
            }
            all_succeeded = False
            if action.on_failure.value == "STOP":
                break
            continue

        valid, errors = validate_single_action(action)
        if not valid:
            receipts[action.action_id] = {
                "status": "error",
                "errors": errors,
                "action_id": action.action_id,
            }
            all_succeeded = False
            if action.on_failure.value == "STOP":
                break
            continue

        try:
            # ── Deterministic failure injection for multi_action_failure ──
            # Scoped by contract_id + action execution order (not module-level state).
            # Always fails the SECOND action in topological order for this contract.
            inject_failure = False
            if scenario_id == "multi_action_failure":
                # Find this action's index in the sorted execution order
                action_index = next(
                    (i for i, a in enumerate(sorted_actions) if a.action_id == action.action_id),
                    -1,
                )
                if action_index == 1:  # Second action (0-indexed)
                    inject_failure = True
                    logger.warning(
                        "execute_approved_actions: INJECTING FAILURE for action_id=%s "
                        "(contract=%s, index=%d in execution order)",
                        action.action_id, contract_id, action_index,
                    )

            if inject_failure:
                import uuid as _uuid
                from datetime import datetime as _dt, timezone as _tz
                result = {
                    "status": "error",
                    "execution_id": _uuid.uuid4().hex[:8],
                    "timestamp": _dt.now(_tz.utc).isoformat(),
                    "service": action.target,
                    "adapter": "simulated",
                    "detail": {
                        "error": (
                            f"Deterministic failure injection: action_id={action.action_id} "
                            f"contract_id={contract_id}"
                        ),
                        "injected_by": "multi_action_failure_scenario",
                        "contract_id": contract_id,
                        "action_id": action.action_id,
                    },
                }
            else:
                result = await _execute_skill_async(
                    skill_name=action.skill.value,
                    arguments=action.arguments,
                )
            receipts[action.action_id] = {
                **result,
                "action_id": action.action_id,
            }
            if result["status"] != "success":
                all_succeeded = False
                if action.on_failure.value == "STOP":
                    break
        except Exception as exc:
            logger.error(
                "Skill execution failed for %s: %s",
                action.action_id, exc,
            )
            receipts[action.action_id] = {
                "status": "error",
                "error": str(exc),
                "action_id": action.action_id,
            }
            all_succeeded = False
            if action.on_failure.value == "STOP":
                break

    # ── Record execution event + room message ────────────────────────────
    if all_succeeded:
        room_content = f"⚡ Execution complete. {len(receipts)}/{len(envelopes)} actions succeeded."
    else:
        success_count = sum(1 for r in receipts.values() if r.get('status') == 'success')
        room_content = f"⚡ Execution finished with failures. {success_count}/{len(envelopes)} actions succeeded."

    await store.append_event_and_room_message(
        incident_id=incident_id,
        run_id=run_id,
        actor="aamil",
        actor_role="executor",
        event_type="actions_executed",
        summary=f"Executed {len(receipts)}/{len(envelopes)} actions",
        payload={
            "receipts": receipts,
            "all_succeeded": all_succeeded,
            "contract_id": contract_id,
        },
        room_sender="aamil",
        room_content=room_content,
        room_message_type="execution",
    )

    # Save to session state for gateway to read
    tool_context.state["execution_receipts"] = receipts
    tool_context.state["all_actions_succeeded"] = all_succeeded
    tool_context.state["contract_id"] = contract_id
    tool_context.state["contract_revision"] = revision

    return {
        "status": "execution_complete",
        "actions_executed": len(receipts),
        "actions_total": len(envelopes),
        "all_succeeded": all_succeeded,
        "contract_id": contract_id,
        "receipts": receipts,
    }


# ────────────────────────────────────────────────────────────────────────────
# Agent factory
# ────────────────────────────────────────────────────────────────────────────

def get_aamil_agent(thinking_level: str = "MINIMAL") -> LlmAgent:
    """Factory: create an Aamil agent with the specified thinking level.

    Args:
        thinking_level: Reasoning allowance — MINIMAL for execution.
            Aamil should not invent or reconsider the plan.
    """
    return LlmAgent(
        name="aamil",
        model=os.getenv("MUHAFIZ_SPEED_MODEL", os.getenv("MUHAFIZ_DEFAULT_MODEL", "gemini-3.1-flash-lite")),
        description=(
            "Executor agent: executes approved remediation actions from "
            "immutable contracts with full audit trail."
        ),
        instruction="""You are Aamil (عامل), The Executor — the operational arm
of the MuhafizSRE autonomous incident response system.

Your mission: safely execute the approved remediation plan.

CRITICAL RULES:
- Execute ONLY from the gateway-validated execution snapshot
- NEVER modify the plan — execute exactly what was approved
- NEVER fetch the contract from the database independently
- Record EVERY action result honestly
- If any step fails, report truthfully

EXECUTION PROCESS:
1. Call `execute_approved_actions` to run all remediation actions.
   - This reads actions from the gateway-validated execution snapshot
   - The gateway is the sole authority for what actions may execute
   - Executes actions in topological (dependency) order
   - Records execution receipts for each action

2. Review the execution results and report what happened.
   The gateway handles recovery verification and finalization.

You are a disciplined executor. You do NOT improvise, negotiate, or
deviate from the approved plan. Execute exactly what was approved
and report honestly.""",
        tools=[
            execute_approved_actions,
        ],
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=thinking_level,
            ),
            max_output_tokens=1500,
        ),
    )


# Default module-level agent for backward compatibility
aamil = get_aamil_agent()

