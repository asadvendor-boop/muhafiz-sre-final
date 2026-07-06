"""
evaluation/mock_executor.py — Mock Agent Executor
===================================================
Replaces _run_single_agent() with a deterministic executor that calls the
REAL agent tool functions (commit_triage, commit_investigation, commit_plan,
commit_verdict, execute_approved_actions) with scripted arguments.

This exercises the actual persistence layer:
  - SQLite status transitions
  - Event chain integrity
  - Room messages
  - Contract issuance & approval

No LLM. No API key. Pure state-machine validation.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from evaluation.mock_responses import MOCK_RESPONSES

logger = logging.getLogger(__name__)


# ─── MockToolContext ──────────────────────────────────────────────────────
# ADK tool functions receive a ToolContext with a `.state` dict-like proxy.
# Our mock provides the same interface backed by a plain dict.

class MockToolContext:
    """Mimics google.adk.tools.ToolContext for mock execution."""

    def __init__(self, state: dict):
        self.state = state


# ─── Mock Agent Runner ────────────────────────────────────────────────────

class MockAgentRunner:
    """Manages per-scenario mock responses and call counting.

    Instantiated once per scenario. Each call to run_agent() consumes
    the next scripted response for that agent name.
    """

    def __init__(self, scenario_id: str):
        self.scenario_id = scenario_id
        responses = MOCK_RESPONSES.get(scenario_id)
        if responses is None:
            raise ValueError(f"No mock responses for scenario: {scenario_id}")
        self._responses = responses
        self._call_counts: dict[str, int] = {}

    def _get_next_response(self, agent_name: str) -> dict | None:
        """Get the next scripted response for an agent.

        Returns None if no response is configured (agent not needed).
        For agents with multiple revisions (list of dicts), returns
        the next one in sequence.

        Raises RuntimeError if scripted responses are exhausted — this
        catches unexpected extra challenge rounds that would otherwise
        be silently hidden.
        """
        self._call_counts[agent_name] = self._call_counts.get(agent_name, 0) + 1
        idx = self._call_counts[agent_name] - 1

        agent_data = self._responses.get(agent_name)
        if agent_data is None:
            return None

        if isinstance(agent_data, list):
            if idx < len(agent_data):
                return agent_data[idx]
            raise RuntimeError(
                f"Mock responses exhausted for {agent_name} in scenario "
                f"{self.scenario_id}: scripted {len(agent_data)} responses "
                f"but call #{idx + 1} requested. This likely indicates an "
                f"unexpected extra challenge round."
            )
        # Single-response agent (not a list) — only valid on first call
        if idx > 0:
            raise RuntimeError(
                f"Mock responses exhausted for {agent_name} in scenario "
                f"{self.scenario_id}: scripted 1 response but call "
                f"#{idx + 1} requested."
            )
        return agent_data

    async def run_agent(self, agent_name: str, state: dict, message: str, **kwargs) -> dict:
        """Execute a mock agent by calling real tool functions.

        This is the drop-in replacement for gateway.app._run_single_agent().
        """
        response = self._get_next_response(agent_name)

        # Aamil doesn't need scripted responses — it reads execution_snapshot
        # from state (injected by gateway before calling the agent).
        if response is None and agent_name != "aamil":
            logger.warning(
                "[MOCK:%s] No response configured for scenario=%s",
                agent_name, self.scenario_id,
            )
            return state

        ctx = MockToolContext(state)
        call_idx = self._call_counts[agent_name]

        logger.info(
            "[MOCK:%s] Executing call #%d for scenario=%s",
            agent_name, call_idx, self.scenario_id,
        )

        if agent_name == "nigehban":
            await self._run_nigehban(ctx, response)
        elif agent_name == "muhaqqiq":
            await self._run_muhaqqiq(ctx, response)
        elif agent_name == "mudabbir":
            await self._run_mudabbir(ctx, response)
        elif agent_name == "muhtasib":
            await self._run_muhtasib(ctx, response)
        elif agent_name == "aamil":
            await self._run_aamil(ctx, response or {})
        else:
            raise ValueError(f"Unknown agent: {agent_name}")

        # Track tools used (for evaluator)
        used = set(state.get("tools_used", []))
        used.update(self._tools_for_agent(agent_name))
        state["tools_used"] = sorted(used)

        return state

    # ── Agent-specific runners ────────────────────────────────────────────

    async def _run_nigehban(self, ctx: MockToolContext, response: dict) -> None:
        """Call real consume_alert + commit_triage."""
        from agents.nigehban import consume_alert, commit_triage

        # Step 1: consume_alert reads alert from state
        await consume_alert(ctx)

        # Step 2: commit_triage with scripted args
        triage_args = response.get("commit_triage", {})
        result = await commit_triage(
            severity=triage_args["severity"],
            service_id=triage_args["service_id"],
            summary=triage_args["summary"],
            is_actionable=triage_args["is_actionable"],
            confidence=triage_args["confidence"],
            tool_context=ctx,
        )
        logger.info("[MOCK:nigehban] commit_triage → %s", result.get("status"))

    async def _run_muhaqqiq(self, ctx: MockToolContext, response: dict) -> None:
        """Call real commit_investigation."""
        from agents.muhaqqiq import commit_investigation

        inv_args = response.get("commit_investigation", {})
        result = await commit_investigation(
            root_cause_code=inv_args["root_cause_code"],
            root_cause_summary=inv_args["root_cause_summary"],
            evidence=inv_args["evidence"],
            confidence=inv_args["confidence"],
            affected_components=inv_args["affected_components"],
            tool_calls_made=inv_args["tool_calls_made"],
            contributing_factors=inv_args["contributing_factors"],
            tool_context=ctx,
        )
        logger.info(
            "[MOCK:muhaqqiq] commit_investigation → %s (root_cause=%s)",
            result.get("status"), inv_args["root_cause_code"],
        )

    async def _run_mudabbir(self, ctx: MockToolContext, response: dict) -> None:
        """Call real commit_plan."""
        from agents.mudabbir import commit_plan

        plan_args = response.get("commit_plan", {})
        result = await commit_plan(
            strategy_summary=plan_args["strategy_summary"],
            risk_level=plan_args["risk_level"],
            estimated_mttr_minutes=plan_args["estimated_mttr_minutes"],
            actions_json=plan_args["actions_json"],
            tool_context=ctx,
        )
        status = result.get("status")
        if status in ("error", "validation_failed"):
            logger.error(
                "[MOCK:mudabbir] commit_plan FAILED: %s", result,
            )
            raise RuntimeError(f"Mock mudabbir commit_plan failed: {result}")
        logger.info(
            "[MOCK:mudabbir] commit_plan → %s (plan_id=%s, actions=%s)",
            status, result.get("plan_id"), result.get("action_count"),
        )

    async def _run_muhtasib(self, ctx: MockToolContext, response: dict) -> None:
        """Call real commit_verdict."""
        from agents.muhtasib import commit_verdict

        verdict_args = response.get("commit_verdict", {})
        result = await commit_verdict(
            decision=verdict_args["decision"],
            risk_score=verdict_args["risk_score"],
            reasoning=verdict_args["reasoning"],
            policy_findings=verdict_args["policy_findings"],
            challenge=verdict_args.get("challenge", ""),
            challenge_target=verdict_args.get("challenge_target", ""),
            tool_context=ctx,
        )
        status = result.get("status")
        if status == "error":
            logger.error(
                "[MOCK:muhtasib] commit_verdict FAILED: %s", result,
            )
            raise RuntimeError(f"Mock muhtasib commit_verdict failed: {result}")
        logger.info(
            "[MOCK:muhtasib] commit_verdict → %s (decision=%s, new_status=%s)",
            status, result.get("decision"), result.get("new_incident_status"),
        )

    async def _run_aamil(self, ctx: MockToolContext, _response: dict) -> None:
        """Call real execute_approved_actions."""
        from agents.aamil import execute_approved_actions

        result = await execute_approved_actions(ctx)
        logger.info(
            "[MOCK:aamil] execute_approved_actions → %s "
            "(executed=%s/%s, all_ok=%s)",
            result.get("status"),
            result.get("actions_executed"),
            result.get("actions_total"),
            result.get("all_succeeded"),
        )

    def _tools_for_agent(self, agent_name: str) -> set[str]:
        """Return tool names that each agent would use.

        MCP tools (get_cloud_logging_traces, etc.) are prefixed with
        'simulated:' because mock mode does not exercise MCP transport.
        Only commit tools are genuinely exercised.
        """
        # Tools genuinely exercised via real function calls
        real_tools = {
            "nigehban": {"consume_alert", "commit_triage"},
            "muhaqqiq": {"commit_investigation"},
            "mudabbir": {"commit_plan"},
            "muhtasib": {"commit_verdict"},
            "aamil": {"execute_approved_actions"},
        }
        # MCP tools declared but not exercised via transport
        simulated_mcp = {
            "muhaqqiq": {
                "simulated:get_cloud_logging_traces",
                "simulated:get_system_metrics",
                "simulated:get_github_deployments",
            },
        }
        tools = real_tools.get(agent_name, set())
        tools |= simulated_mcp.get(agent_name, set())
        return tools

    @property
    def call_counts(self) -> dict[str, int]:
        return dict(self._call_counts)


# ─── Integration helper ──────────────────────────────────────────────────

def create_mock_agent_runner(scenario_id: str) -> MockAgentRunner:
    """Factory for MockAgentRunner. Called by the evaluation runner."""
    return MockAgentRunner(scenario_id)
