"""
agents/muhtasib.py – Muhtasib (The Safety Inspector) Review Agent
========================================================================

Adversarial safety reviewer in the MuhafizSRE pipeline. Performs
zero-trust verification of remediation plans before they reach
human approval. Issues structured SafetyVerdict decisions.

ADK 2.x conventions:
    - Async tool functions with ToolContext
    - SafetyDecision enum for verdict classification
    - Atomic commit via IncidentStore.commit_agent_decision()
    - Hash-chained event persistence

§13.6 – Safety review stage contract.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext
from google.genai import types


# ────────────────────────────────────────────────────────────────────────────
# Safety policy document
# ────────────────────────────────────────────────────────────────────────────

SAFETY_POLICIES = """
## MuhafizSRE Safety Policy Rules

### CRITICAL (Auto-Block if violated):
1. NO database DROP, TRUNCATE, or DELETE operations.
2. NO credential rotation during active P0/P1 unless credentials ARE the root cause.
3. NO scaling below 1 replica for any production service.
4. NO new code deployments — rollback ONLY.
5. NO single action affecting more than one service simultaneously.
6. ALL actions MUST use allowlisted skills and target allowlisted services.

### HIGH (Challenge if not justified):
7. Rolling restarts during peak hours require justification.
8. Cache flushes of shared caches must document downstream impact.
9. Rate limits below 100 RPS require business justification.
10. Multiple sequential actions on same service need ordering rationale.

### EVIDENCE QUALITY:
11. Root cause must be supported by evidence from at least 2 data sources.
12. Confidence below 0.5 should trigger ESCALATE, not automated remediation.
13. UNKNOWN root cause code requires human escalation.

### REVERSIBILITY:
14. Every action in the plan MUST be reversible.
15. Estimated MTTR must be realistic (>= 2 minutes for infrastructure changes).
"""


# ────────────────────────────────────────────────────────────────────────────
# Tool: commit_verdict
# ────────────────────────────────────────────────────────────────────────────

async def commit_verdict(
    decision: str,
    risk_score: float,
    reasoning: str,
    policy_findings: str,
    challenge: str,
    challenge_target: str,
    tool_context: ToolContext,
) -> dict:
    """Atomically commit the safety verdict to the incident store.

    Issues a SafetyVerdict with one of four decisions, each mapping
    to a specific incident status transition:

    - APPROVED_REQUIRES_HUMAN → AWAITING_APPROVAL (proceeds to human gate)
    - CHALLENGE → REVIEWING (plan sent back to Mudabbir for revision)
    - BLOCKED_UNSAFE → BLOCKED (plan is rejected as unsafe)
    - ESCALATE → ESCALATED (requires human SRE intervention)

    Args:
        decision: One of: APPROVED_REQUIRES_HUMAN, CHALLENGE,
            BLOCKED_UNSAFE, ESCALATE.
        risk_score: Quantified risk score (0.0 = safe, 1.0 = catastrophic).
        reasoning: Detailed safety reasoning supporting the decision.
        policy_findings: Comma-separated list of policy findings/violations.
        challenge: Specific challenge or objection text (if CHALLENGE).
            Use "none" if not challenging.
        challenge_target: What the challenge targets — "EVIDENCE" or "PLAN".
            Use "none" if not challenging.
        tool_context: ADK-injected context with session state.

    Returns:
        dict confirming the commit with event_hash and resulting status.
    """
    from shared.dependencies import get_store
    store = get_store()
    incident_id = tool_context.state.get("incident_id")
    run_id = tool_context.state.get("run_id")

    # Parse policy findings
    findings = [f.strip() for f in policy_findings.split(",") if f.strip() and f.strip().lower() != "none"]

    # Map decision to incident status
    # CHALLENGE transitions depend on target: re-investigate or re-plan
    if decision == "CHALLENGE":
        if challenge_target.upper() == "EVIDENCE":
            challenge_status = "ANALYZING"  # back to Muhaqqiq
        else:
            challenge_status = "PLANNING"   # back to Mudabbir
    else:
        challenge_status = None

    decision_status_map = {
        "APPROVED_REQUIRES_HUMAN": "AWAITING_APPROVAL",
        "CHALLENGE": challenge_status or "PLANNING",
        "BLOCKED_UNSAFE": "BLOCKED",
        "ESCALATE": "ESCALATED",
    }

    if decision not in decision_status_map:
        return {
            "status": "error",
            "errors": [
                f"Invalid decision '{decision}'. Must be one of: "
                f"{list(decision_status_map.keys())}"
            ],
        }

    new_status = decision_status_map[decision]

    # Build verdict payload — include retry tracking from session state
    first_pass_commit = tool_context.state.get("first_pass_commit", True)
    retry_used = tool_context.state.get("retry_used", False)
    verdict = {
        "decision": decision,
        "risk_score": risk_score,
        "reasoning": reasoning,
        "policy_findings": findings,
        "challenge": challenge if challenge.lower() != "none" else None,
        "challenge_target": challenge_target if challenge_target.lower() != "none" else None,
        "first_pass_commit": first_pass_commit,
        "retry_used": retry_used,
    }

    # Build decision-specific room message
    rationale = reasoning
    room_mentions: list[str] | None = None
    if decision == "APPROVED_REQUIRES_HUMAN":
        room_content = "⚖️ Safety review PASSED. Plan is safe for execution. Awaiting human authorization."
    elif decision == "CHALLENGE" and challenge_target.upper() == "EVIDENCE":
        room_content = f"⚖️ CHALLENGE: Evidence insufficient — {rationale.rstrip('. ')}. @muhaqqiq, provide additional data."
        room_mentions = ["muhaqqiq"]
    elif decision == "CHALLENGE" and challenge_target.upper() == "PLAN":
        room_content = f"⚖️ CHALLENGE: Plan revision needed — {rationale.rstrip('. ')}. @mudabbir, address this concern."
        room_mentions = ["mudabbir"]
    elif decision == "BLOCKED_UNSAFE":
        room_content = f"⚖️ BLOCKED: Plan is unsafe. {rationale}. Incident escalated."
    elif decision == "ESCALATE":
        room_content = f"⚖️ ESCALATED: {rationale}."
    else:
        room_content = f"⚖️ Verdict: {decision}. {rationale}."

    # Atomic commit: event + room message + status transition
    event, _room_msg = await store.append_event_and_room_message(
        incident_id=incident_id,
        run_id=run_id,
        actor="muhtasib",
        actor_role="safety_reviewer",
        event_type="verdict_issued",
        summary=f"Safety verdict: {decision} (risk={risk_score:.2f})",
        payload=verdict,
        room_sender="muhtasib",
        room_content=room_content,
        room_mentions=room_mentions,
        room_message_type="verdict",
        transition_from="REVIEWING",
        transition_to=new_status,
    )

    # Save to session state for downstream agents
    tool_context.state["verdict"] = verdict
    tool_context.state["verdict_event_hash"] = event["event_hash"]

    return {
        "status": "verdict_committed",
        "decision": decision,
        "new_incident_status": new_status,
        "event_hash": event["event_hash"],
        "risk_score": risk_score,
        "next_stage": {
            "APPROVED_REQUIRES_HUMAN": "human_approval_gate",
            "CHALLENGE": "planning_revision",
            "BLOCKED_UNSAFE": "closed_blocked",
            "ESCALATE": "human_escalation",
        }[decision],
    }


# ────────────────────────────────────────────────────────────────────────────
# Agent factory
# ────────────────────────────────────────────────────────────────────────────

def get_muhtasib_agent(thinking_level: str = "HIGH") -> LlmAgent:
    """Factory: create a Muhtasib agent with the specified thinking level.

    Args:
        thinking_level: Reasoning allowance — HIGH for all safety reviews.
            The safety-critical agent always uses the strongest reasoning.
    """
    return LlmAgent(
        name="muhtasib",
        model=os.getenv("MUHAFIZ_SAFETY_MODEL", os.getenv("MUHAFIZ_DEFAULT_MODEL", "gemini-3.1-pro-preview")),
        description=(
            "Adversarial safety reviewer: verifies remediation plans against "
            "safety policies before human approval."
        ),
        instruction=f"""You are Muhtasib (محتسب), The Safety Inspector — the zero-trust
compliance officer of the MuhafizSRE autonomous incident response system.

Your mission: ADVERSARIALLY review the remediation plan for safety.

CONTEXT AVAILABLE IN SESSION STATE:
- triage_result: Nigehban's severity classification
- investigation_result: Muhaqqiq's root cause analysis and evidence
- plan: Mudabbir's remediation plan with typed action envelopes

SAFETY POLICIES TO ENFORCE:
{SAFETY_POLICIES}

REVIEW PROCESS:
1. Read the triage_result, investigation_result, and plan from the conversation.
2. Systematically verify EVERY aspect:

   a. EVIDENCE QUALITY: Is the root cause diagnosis well-supported?
      - Were all 3 MCP tools used (logs, metrics, deployments)?
      - Is confidence >= 0.5?
      - Does evidence from multiple sources converge?

   b. PLAN SAFETY: Are all actions safe and bounded?
      - Do all actions use allowlisted skills and targets?
      - Are arguments within bounded ranges?
      - No forbidden operations?

   c. BLAST RADIUS: Could this cause collateral damage?
      - Single-service scope?
      - Failure policy appropriate?

   d. REVERSIBILITY: Can every action be undone?
      - Rollback procedures documented?
      - MTTR realistic?

   e. PROPORTIONALITY: Is the response proportional to the incident?
      - Not over-remediating a minor issue?
      - Not under-remediating a critical one?

3. Issue your verdict by calling `commit_verdict`:

   - APPROVED_REQUIRES_HUMAN: Plan is safe, send to human for final approval.
     Use when: evidence is strong, actions are bounded, risk is acceptable.

   - CHALLENGE: Send plan back to Mudabbir for revision.
     Use when: plan has fixable issues, evidence gaps, or suboptimal strategy.
     Set challenge_target to "EVIDENCE" or "PLAN".

   - BLOCKED_UNSAFE: Block the plan as unsafe.
     Use when: critical policy violations, dangerous actions, unacceptable risk.

   - ESCALATE: Require human SRE intervention.
     Use when: UNKNOWN root cause, confidence < 0.5, unprecedented situation.

Be adversarial but fair. Don't reject reasonable plans just because they
involve risk — incident response always involves risk. Focus on catching
genuine safety violations and evidence weaknesses.""",
        tools=[commit_verdict],
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=thinking_level,
            ),
            max_output_tokens=1500,
        ),
    )


# Default module-level agent for backward compatibility
muhtasib = get_muhtasib_agent()

