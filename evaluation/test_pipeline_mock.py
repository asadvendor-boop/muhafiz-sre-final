"""
Mock Pipeline Integration Tests
================================
Tests the gateway pipeline state machine with deterministic mock agent responses.
No LLM calls. No API keys needed. Validates:
  - Status transitions (DETECTED → ANALYZING → PLANNING → REVIEWING → ...)
  - Challenge loops (PLAN challenge → Mudabbir revision → re-review)
  - Forced commit finalizer
  - Phase 2 execution & recovery verification
  - Contract issuance & approval
  - Terminal states: RESOLVED, DEGRADED, ESCALATED, EXECUTION_FAILED

Usage:
  python -m evaluation.test_pipeline_mock
"""

import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

# Project root on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("GEMINI_API_KEY", "mock-not-needed")
os.environ.setdefault("MUHAFIZ_DEFAULT_MODEL", "gemini-2.0-flash")

from gateway.models import Alert, Severity


# ─── Mock Agent Response Factories ────────────────────────────────────────

def _hash(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def mock_nigehban_triage(state: dict, message: str) -> dict:
    """Simulates Nigehban consuming alert and committing triage."""
    alert = state.get("alert", {})
    state["triage_result"] = {
        "is_actionable": True,
        "service_id": alert.get("service_id", "test-service"),
        "severity": alert.get("severity", "P1"),
        "summary": alert.get("summary", "Test alert"),
        "alert_type": alert.get("alert_type", "error_rate"),
        "error_message": alert.get("error_message", "Test error"),
    }
    state["triage_event_hash"] = _hash("triage")
    state["tools_used"] = state.get("tools_used", []) + ["consume_alert", "commit_triage"]
    return state


def mock_muhaqqiq_investigation(state: dict, message: str, root_cause: str = "BAD_DEPLOYMENT") -> dict:
    """Simulates Muhaqqiq investigating and committing investigation."""
    state["investigation_result"] = {
        "root_cause_code": root_cause,
        "root_cause_summary": f"Mock: {root_cause}",
        "evidence": [{"source": "mock", "data": "test", "trust": "direct"}],
        "confidence": 0.95,
        "affected_components": [state.get("alert", {}).get("service_id", "test")],
        "tool_calls_made": ["get_cloud_logging_traces", "get_github_deployments", "get_system_metrics"],
        "contributing_factors": ["mock_test"],
    }
    state["investigation_event_hash"] = _hash(f"investigation-{root_cause}")
    state["tools_used"] = list(set(state.get("tools_used", []) + [
        "fetch_telemetry", "commit_investigation",
        "get_cloud_logging_traces", "get_github_deployments", "get_system_metrics",
    ]))
    return state


def mock_muhaqqiq_skip_commit(state: dict, message: str) -> dict:
    """Simulates Muhaqqiq analyzing but FORGETTING to call commit_investigation."""
    # Only adds tool calls, no investigation_result
    state["tools_used"] = list(set(state.get("tools_used", []) + [
        "fetch_telemetry", "get_cloud_logging_traces", "get_system_metrics",
    ]))
    return state


def mock_mudabbir_plan(state: dict, message: str, actions: list | None = None, plan_id: str | None = None) -> dict:
    """Simulates Mudabbir committing a plan."""
    if actions is None:
        actions = [{"action_id": "act-1", "skill": "rollback_service_revision", "target": "test-service"}]

    pid = plan_id or f"PLAN-{uuid.uuid4().hex[:8].upper()}"
    plan = {
        "plan_id": pid,
        "revision": state.get("plan_revision", 1),
        "risk_level": "medium",
        "estimated_mttr_minutes": 5,
        "strategy_summary": "Mock plan",
        "actions": actions,
    }
    state["plan"] = plan
    state["plan_hash"] = _hash(json.dumps(plan))
    state["plan_event_hash"] = _hash(f"plan-{pid}")
    state["tools_used"] = list(set(state.get("tools_used", []) + ["commit_plan"]))
    return state


def mock_mudabbir_skip_commit(state: dict, message: str) -> dict:
    """Simulates Mudabbir not committing a plan."""
    return state


def mock_muhtasib_approve(state: dict, message: str) -> dict:
    """Simulates Muhtasib approving the plan."""
    state["verdict"] = {
        "decision": "APPROVED_REQUIRES_HUMAN",
        "risk_score": 0.2,
        "reasoning": "Plan looks safe",
        "challenge_target": "none",
    }
    state["verdict_event_hash"] = _hash("verdict-approved")
    state["tools_used"] = list(set(state.get("tools_used", []) + ["commit_verdict"]))
    return state


def mock_muhtasib_challenge_plan(state: dict, message: str) -> dict:
    """Simulates Muhtasib challenging the plan."""
    state["verdict"] = {
        "decision": "CHALLENGE",
        "risk_score": 0.6,
        "reasoning": "Missing flush_cache action",
        "challenge_target": "PLAN",
        "challenge": "Add flush_cache to remediation plan",
    }
    state["verdict_event_hash"] = _hash("verdict-challenge")
    state["tools_used"] = list(set(state.get("tools_used", []) + ["commit_verdict"]))
    return state


def mock_muhtasib_challenge_evidence(state: dict, message: str) -> dict:
    """Simulates Muhtasib challenging the evidence."""
    state["verdict"] = {
        "decision": "CHALLENGE",
        "risk_score": 0.5,
        "reasoning": "Insufficient evidence for root cause",
        "challenge_target": "EVIDENCE",
        "challenge": "Re-investigate with more data",
    }
    state["verdict_event_hash"] = _hash("verdict-challenge-evidence")
    state["tools_used"] = list(set(state.get("tools_used", []) + ["commit_verdict"]))
    return state


def mock_aamil_execute_all_success(state: dict, message: str) -> dict:
    """Simulates Aamil executing all actions successfully."""
    snapshot = state.get("execution_snapshot", {})
    actions = snapshot.get("actions", [])
    receipts = {}
    for act in actions:
        aid = act.get("action_id", "unknown")
        receipts[aid] = {"status": "success", "output": f"Mock executed {aid}"}
    state["execution_receipts"] = receipts
    state["all_actions_succeeded"] = True
    state["reconciliation"] = {
        "status": "all_succeeded",
        "receipts": receipts,
        "succeeded": len(receipts),
        "total": len(receipts),
    }
    state["tools_used"] = list(set(state.get("tools_used", []) + ["execute_approved_actions"]))
    return state


def mock_aamil_execute_partial(state: dict, message: str) -> dict:
    """Simulates Aamil with partial execution (one succeeds, one fails)."""
    snapshot = state.get("execution_snapshot", {})
    actions = snapshot.get("actions", [])
    receipts = {}
    for i, act in enumerate(actions):
        aid = act.get("action_id", f"act-{i}")
        if i == 0:
            receipts[aid] = {"status": "success", "output": f"Mock executed {aid}"}
        else:
            receipts[aid] = {"status": "error", "error": f"Mock failed {aid}"}
    succeeded = sum(1 for r in receipts.values() if r["status"] == "success")
    state["execution_receipts"] = receipts
    state["all_actions_succeeded"] = False
    state["reconciliation"] = {
        "status": "partial" if succeeded > 0 else "all_failed",
        "receipts": receipts,
        "succeeded": succeeded,
        "total": len(receipts),
    }
    state["tools_used"] = list(set(state.get("tools_used", []) + ["execute_approved_actions"]))
    return state


# ─── Test Scenarios ───────────────────────────────────────────────────────

class MockPipelineTest:
    """Runs pipeline tests with mock agent responses."""

    def __init__(self):
        self.results: list[dict] = []

    async def run_all(self):
        print("=" * 60)
        print("MOCK PIPELINE TESTS (no LLM calls)")
        print("=" * 60)

        tests = [
            ("happy_path_resolved", self.test_happy_path_resolved),
            ("forced_commit_finalizer", self.test_forced_commit_finalizer),
            ("plan_challenge_then_approve", self.test_plan_challenge_then_approve),
            ("evidence_challenge_then_approve", self.test_evidence_challenge_then_approve),
            ("max_challenges_escalation", self.test_max_challenges_escalation),
            ("mudabbir_skip_commit", self.test_mudabbir_skip_commit),
            ("partial_execution_degraded", self.test_partial_execution_degraded),
            ("bad_deployment_full_flow", self.test_bad_deployment_scenario),
            ("cache_stampede_full_flow", self.test_cache_stampede_scenario),
            ("expired_credential_challenge", self.test_expired_credential_scenario),
            ("multi_action_partial", self.test_multi_action_failure_scenario),
        ]

        passed = 0
        failed = 0
        for name, test_fn in tests:
            try:
                await test_fn()
                print(f"  ✅ {name}")
                passed += 1
                self.results.append({"name": name, "status": "PASS"})
            except Exception as e:
                print(f"  ❌ {name}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
                self.results.append({"name": name, "status": "FAIL", "error": str(e)})

        print()
        print("=" * 60)
        print(f"RESULTS: {passed}/{passed + failed} passed")
        print("=" * 60)
        return passed, failed

    def _make_alert(self, service_id="payment-gateway", alert_type="error_rate",
                    error_message="Connection pool exhausted", severity="P1",
                    summary="Service degradation detected"):
        return Alert(
            service_id=service_id,
            alert_type=alert_type,
            error_message=error_message,
            severity=Severity(severity),
            summary=summary,
        )

    async def _run_with_mocks(self, alert: Alert, agent_responses: dict,
                              scenario_id: str = "test"):
        """Run pipeline with mocked _run_single_agent responses.

        agent_responses: dict mapping agent_name to a callable or list of callables.
        If a list, each call to the agent pops the next response.
        """
        import secrets as _s
        from gateway.app import _run_phase1_pipeline, _run_phase2_execution
        from gateway.security import Settings, ApprovalTokenManager
        from gateway.store import IncidentStore
        from shared.dependencies import init_dependencies

        store = IncidentStore()
        await store.initialize()

        settings = Settings.from_env()
        if not settings.approval_secret or len(settings.approval_secret) < 32:
            settings.approval_secret = _s.token_hex(32)
        token_manager = ApprovalTokenManager(settings.approval_secret)
        init_dependencies(store=store, token_manager=token_manager, settings=settings)

        # Status transitions that real agent tools perform
        _AGENT_TRANSITIONS = {
            "nigehban": ("DETECTED", "ANALYZING"),
            "muhaqqiq": ("ANALYZING", "PLANNING"),
            "mudabbir": ("PLANNING", "REVIEWING"),
            # muhtasib transitions are handled by commit_verdict logic
        }

        # Track call counts per agent
        call_counts: dict[str, int] = {}

        async def mock_run_single_agent(agent_name, state, message):
            call_counts[agent_name] = call_counts.get(agent_name, 0) + 1
            idx = call_counts[agent_name] - 1

            handler = agent_responses.get(agent_name)
            if handler is None:
                raise RuntimeError(f"No mock response for agent: {agent_name}")

            if isinstance(handler, list):
                if idx < len(handler):
                    fn = handler[idx]
                else:
                    fn = handler[-1]  # repeat last
            else:
                fn = handler

            result_state = fn(state, message)
            inc_id = result_state.get("incident_id", incident_id)
            run_id_local = result_state.get("run_id", "")

            # Perform store transitions that the real tools would do
            if agent_name in _AGENT_TRANSITIONS:
                from_s, to_s = _AGENT_TRANSITIONS[agent_name]
                # Only transition if the mock actually "committed" something
                committed = False
                if agent_name == "nigehban" and "triage_result" in result_state:
                    committed = True
                elif agent_name == "muhaqqiq" and "investigation_result" in result_state:
                    committed = True
                elif agent_name == "mudabbir" and "plan" in result_state:
                    committed = True

                if committed:
                    try:
                        await store.transition_incident(inc_id, from_s, to_s)
                        # Also append the event for chain integrity
                        event_type_map = {
                            "nigehban": "triage_completed",
                            "muhaqqiq": "investigation_completed",
                            "mudabbir": "plan_committed",
                        }
                        await store.append_event(
                            incident_id=inc_id, run_id=run_id_local,
                            actor=agent_name, actor_role="mock",
                            event_type=event_type_map.get(agent_name, "mock_event"),
                            summary=f"Mock {agent_name} completed",
                            payload={},
                        )
                    except Exception:
                        pass  # Transition may already be done
            elif agent_name == "muhtasib":
                verdict = result_state.get("verdict", {})
                decision = verdict.get("decision", "")
                if decision == "APPROVED_REQUIRES_HUMAN":
                    # Muhtasib stays in REVIEWING — gateway handles transition to AWAITING_APPROVAL
                    await store.append_event(
                        incident_id=inc_id, run_id=run_id_local,
                        actor="muhtasib", actor_role="mock",
                        event_type="verdict_committed",
                        summary=f"Mock verdict: {decision}",
                        payload=verdict,
                    )
                elif decision == "CHALLENGE":
                    # Challenge stays in REVIEWING — plan revision needed
                    result_state["challenge_feedback"] = verdict.get("reasoning", "")
                    await store.append_event(
                        incident_id=inc_id, run_id=run_id_local,
                        actor="muhtasib", actor_role="mock",
                        event_type="plan_challenged",
                        summary=f"Mock challenge: {verdict.get('challenge_target', 'PLAN')}",
                        payload=verdict,
                    )

            return result_state

        incident_id = f"MOCK-{uuid.uuid4().hex[:8].upper()}"
        os.environ["MUHAFIZ_SCENARIO_ID"] = scenario_id

        # Create incident
        await store.create_incident(
            incident_id=incident_id,
            alert=alert,
            scenario_id=scenario_id,
        )

        # Claim pipeline run
        run = await store.claim_pipeline_run(
            incident_id=incident_id,
            phase="phase1",
            revision=1,
            start_stage="triage",
            input_data={"alert": alert.model_dump(mode="json")},
        )
        run_id = run["run_id"]

        # Run Phase 1 with mock
        with patch("gateway.app._run_single_agent", side_effect=mock_run_single_agent):
            await _run_phase1_pipeline(incident_id, run_id, alert)

        # Check if we need Phase 2
        incident = await store.get_incident(incident_id)
        status = incident.get("status", "") if incident else ""

        if status == "AWAITING_APPROVAL":
            contract = await store.get_active_contract(incident_id)
            if contract:
                # Approve via production path
                from evaluation.runner import _approve_via_production_path

                claimed = await _approve_via_production_path(
                    incident_id=incident_id,
                    run_id=run_id,
                    contract=contract,
                    store=store,
                    token_manager=token_manager,
                )

                if claimed:
                    # Mock recovery verifier to always succeed
                    with patch("gateway.app._run_single_agent", side_effect=mock_run_single_agent), \
                         patch("shared.recovery_verifier.verify_recovery", new_callable=AsyncMock,
                               return_value={"status": "RECOVERED", "recovery_score": 1.0, "checks": []}):
                        await _run_phase2_execution(incident_id, run_id, contract)

        # Get final state
        final_incident = await store.get_incident(incident_id)
        events = await store.get_events(incident_id)

        return {
            "incident_id": incident_id,
            "status": final_incident.get("status", "UNKNOWN") if final_incident else "UNKNOWN",
            "events": events,
            "event_count": len(events),
            "call_counts": call_counts,
        }

    # ── Individual Tests ──────────────────────────────────────────────────

    async def test_happy_path_resolved(self):
        """Nigehban → Muhaqqiq → Mudabbir → Muhtasib(approve) → Phase2 → RESOLVED"""
        result = await self._run_with_mocks(
            alert=self._make_alert(),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": lambda s, m: mock_muhaqqiq_investigation(s, m, "BAD_DEPLOYMENT"),
                "mudabbir": mock_mudabbir_plan,
                "muhtasib": mock_muhtasib_approve,
                "aamil": mock_aamil_execute_all_success,
            },
        )
        assert result["status"] == "RESOLVED", f"Expected RESOLVED, got {result['status']}"
        assert result["event_count"] >= 6, f"Expected >=6 events, got {result['event_count']}"

    async def test_forced_commit_finalizer(self):
        """Muhaqqiq skips commit → forced commit fires → pipeline continues → RESOLVED"""
        result = await self._run_with_mocks(
            alert=self._make_alert(
                service_id="auth-service",
                alert_type="auth_failure",
                error_message="API key expired: key-prod-2024 TTL exceeded",
                summary="Spike in 401 Unauthorized responses, API key validation failing",
            ),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": mock_muhaqqiq_skip_commit,  # No commit!
                "mudabbir": lambda s, m: mock_mudabbir_plan(s, m, actions=[
                    {"action_id": "rotate-1", "skill": "rotate_credentials", "target": "auth-service"}
                ]),
                "muhtasib": mock_muhtasib_approve,
                "aamil": mock_aamil_execute_all_success,
            },
        )
        assert result["status"] == "RESOLVED", f"Expected RESOLVED, got {result['status']}"
        # Verify forced commit used EXPIRED_CREDENTIAL root cause
        inv_events = [e for e in result["events"] if e.get("event_type") == "investigation_completed"]
        assert len(inv_events) >= 1, "No investigation_completed event found"
        payload = inv_events[0].get("payload") or inv_events[0].get("payload_json", "{}")
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload.get("root_cause_code") == "EXPIRED_CREDENTIAL", \
            f"Forced commit should infer EXPIRED_CREDENTIAL, got {payload.get('root_cause_code')}"

    async def test_plan_challenge_then_approve(self):
        """Muhtasib challenges plan → Mudabbir revises → approve → RESOLVED"""
        mudabbir_call = [0]

        def mudabbir_factory(state, message):
            mudabbir_call[0] += 1
            if mudabbir_call[0] == 1:
                return mock_mudabbir_plan(state, message, actions=[
                    {"action_id": "act-1", "skill": "scale_service", "target": "payment-gateway"}
                ])
            else:
                # Revised plan with flush_cache added
                return mock_mudabbir_plan(state, message, actions=[
                    {"action_id": "act-1", "skill": "scale_service", "target": "payment-gateway"},
                    {"action_id": "act-2", "skill": "flush_cache", "target": "payment-gateway"},
                ], plan_id=f"PLAN-REV{mudabbir_call[0]}")

        muhtasib_call = [0]

        def muhtasib_factory(state, message):
            muhtasib_call[0] += 1
            if muhtasib_call[0] == 1:
                return mock_muhtasib_challenge_plan(state, message)
            else:
                return mock_muhtasib_approve(state, message)

        result = await self._run_with_mocks(
            alert=self._make_alert(),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": lambda s, m: mock_muhaqqiq_investigation(s, m, "CACHE_STAMPEDE"),
                "mudabbir": mudabbir_factory,
                "muhtasib": muhtasib_factory,
                "aamil": mock_aamil_execute_all_success,
            },
        )
        assert result["status"] == "RESOLVED", f"Expected RESOLVED, got {result['status']}"
        assert result["call_counts"].get("mudabbir", 0) == 2, \
            f"Mudabbir should be called twice (initial + revision), got {result['call_counts'].get('mudabbir')}"

    async def test_evidence_challenge_then_approve(self):
        """Muhtasib challenges evidence → Muhaqqiq re-investigates → Mudabbir re-plans → approve"""
        muhaqqiq_call = [0]

        def muhaqqiq_factory(state, message):
            muhaqqiq_call[0] += 1
            if muhaqqiq_call[0] == 1:
                return mock_muhaqqiq_investigation(state, message, "UNKNOWN")
            else:
                return mock_muhaqqiq_investigation(state, message, "BAD_DEPLOYMENT")

        mudabbir_call = [0]

        def mudabbir_factory(state, message):
            mudabbir_call[0] += 1
            return mock_mudabbir_plan(state, message, plan_id=f"PLAN-{mudabbir_call[0]}")

        muhtasib_call = [0]

        def muhtasib_factory(state, message):
            muhtasib_call[0] += 1
            if muhtasib_call[0] == 1:
                return mock_muhtasib_challenge_evidence(state, message)
            else:
                return mock_muhtasib_approve(state, message)

        result = await self._run_with_mocks(
            alert=self._make_alert(),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": muhaqqiq_factory,
                "mudabbir": mudabbir_factory,
                "muhtasib": muhtasib_factory,
                "aamil": mock_aamil_execute_all_success,
            },
        )
        assert result["status"] == "RESOLVED", f"Expected RESOLVED, got {result['status']}"
        assert muhaqqiq_call[0] == 2, "Muhaqqiq should be called twice (initial + re-investigate)"
        assert mudabbir_call[0] == 2, "Mudabbir should be called twice (initial + re-plan)"

    async def test_max_challenges_escalation(self):
        """3 challenge rounds exceeded → ESCALATED"""
        result = await self._run_with_mocks(
            alert=self._make_alert(),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": lambda s, m: mock_muhaqqiq_investigation(s, m, "CACHE_STAMPEDE"),
                "mudabbir": mock_mudabbir_plan,  # Always commits a new plan
                "muhtasib": mock_muhtasib_challenge_plan,  # Always challenges
            },
        )
        assert result["status"] == "ESCALATED", f"Expected ESCALATED, got {result['status']}"

    async def test_mudabbir_skip_commit(self):
        """Mudabbir doesn't commit plan → PIPELINE_FAILED"""
        result = await self._run_with_mocks(
            alert=self._make_alert(),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": lambda s, m: mock_muhaqqiq_investigation(s, m, "BAD_DEPLOYMENT"),
                "mudabbir": mock_mudabbir_skip_commit,
            },
        )
        assert result["status"] == "PIPELINE_FAILED", f"Expected PIPELINE_FAILED, got {result['status']}"

    async def test_partial_execution_degraded(self):
        """Aamil partially executes → DEGRADED"""
        result = await self._run_with_mocks(
            alert=self._make_alert(),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": lambda s, m: mock_muhaqqiq_investigation(s, m, "CACHE_STAMPEDE"),
                "mudabbir": lambda s, m: mock_mudabbir_plan(s, m, actions=[
                    {"action_id": "act-1", "skill": "flush_cache", "target": "payment-gateway"},
                    {"action_id": "act-2", "skill": "scale_service", "target": "payment-gateway"},
                ]),
                "muhtasib": mock_muhtasib_approve,
                "aamil": mock_aamil_execute_partial,
            },
        )
        assert result["status"] == "DEGRADED", f"Expected DEGRADED, got {result['status']}"

    # ── Scenario-Specific Tests (mirror evaluation scenarios) ──────────

    async def test_bad_deployment_scenario(self):
        """bad_deployment: rollback → RESOLVED"""
        result = await self._run_with_mocks(
            alert=self._make_alert(
                service_id="payment-gateway",
                alert_type="error_rate",
                error_message="HTTP 500 surge after deploy_abc123",
                summary="Payment service errors after deployment",
            ),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": lambda s, m: mock_muhaqqiq_investigation(s, m, "BAD_DEPLOYMENT"),
                "mudabbir": lambda s, m: mock_mudabbir_plan(s, m, actions=[
                    {"action_id": "rollback-1", "skill": "rollback_service_revision",
                     "target": "payment-gateway", "arguments": {"target_revision": "deploy_prev"}}
                ]),
                "muhtasib": mock_muhtasib_approve,
                "aamil": mock_aamil_execute_all_success,
            },
            scenario_id="bad_deployment",
        )
        assert result["status"] == "RESOLVED", f"Expected RESOLVED, got {result['status']}"

    async def test_cache_stampede_scenario(self):
        """cache_stampede: flush_cache + scale_service → RESOLVED"""
        result = await self._run_with_mocks(
            alert=self._make_alert(
                service_id="payment-gateway",
                alert_type="error_rate",
                error_message="ConnectionPool exhausted, Redis timeout, 50% of requests failing",
                summary="Payment processing failures with database and cache issues",
            ),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": lambda s, m: mock_muhaqqiq_investigation(s, m, "CACHE_STAMPEDE"),
                "mudabbir": lambda s, m: mock_mudabbir_plan(s, m, actions=[
                    {"action_id": "flush-1", "skill": "flush_cache", "target": "payment-gateway"},
                    {"action_id": "scale-1", "skill": "scale_service", "target": "payment-gateway"},
                ]),
                "muhtasib": mock_muhtasib_approve,
                "aamil": mock_aamil_execute_all_success,
            },
            scenario_id="cache_stampede",
        )
        assert result["status"] == "RESOLVED", f"Expected RESOLVED, got {result['status']}"

    async def test_expired_credential_scenario(self):
        """expired_credential: forced commit + challenge + rotate → RESOLVED"""
        muhtasib_call = [0]

        def muhtasib_factory(state, message):
            muhtasib_call[0] += 1
            if muhtasib_call[0] == 1:
                return mock_muhtasib_challenge_plan(state, message)
            return mock_muhtasib_approve(state, message)

        mudabbir_call = [0]

        def mudabbir_factory(state, message):
            mudabbir_call[0] += 1
            return mock_mudabbir_plan(state, message, actions=[
                {"action_id": "rotate-1", "skill": "rotate_credentials", "target": "auth-service"}
            ], plan_id=f"PLAN-{mudabbir_call[0]}")

        result = await self._run_with_mocks(
            alert=self._make_alert(
                service_id="auth-service",
                alert_type="auth_failure",
                error_message="API key expired: key-prod-2024 TTL exceeded",
                summary="Spike in 401 Unauthorized responses, API key validation failing",
            ),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": mock_muhaqqiq_skip_commit,  # Forced commit activates
                "mudabbir": mudabbir_factory,
                "muhtasib": muhtasib_factory,
                "aamil": mock_aamil_execute_all_success,
            },
            scenario_id="expired_credential",
        )
        assert result["status"] == "RESOLVED", f"Expected RESOLVED, got {result['status']}"

    async def test_multi_action_failure_scenario(self):
        """multi_action_failure: partial execution → DEGRADED"""
        result = await self._run_with_mocks(
            alert=self._make_alert(
                service_id="payment-gateway",
                alert_type="error_rate",
                error_message="ConnectionPool exhausted, Redis timeout, 50% of requests failing",
                summary="Payment processing failures with database and cache issues",
            ),
            agent_responses={
                "nigehban": mock_nigehban_triage,
                "muhaqqiq": lambda s, m: mock_muhaqqiq_investigation(s, m, "CACHE_STAMPEDE"),
                "mudabbir": lambda s, m: mock_mudabbir_plan(s, m, actions=[
                    {"action_id": "flush-1", "skill": "flush_cache", "target": "payment-gateway"},
                    {"action_id": "scale-1", "skill": "scale_service", "target": "payment-gateway"},
                ]),
                "muhtasib": mock_muhtasib_approve,
                "aamil": mock_aamil_execute_partial,  # Partial failure
            },
            scenario_id="multi_action_failure",
        )
        assert result["status"] == "DEGRADED", f"Expected DEGRADED, got {result['status']}"


async def main():
    tester = MockPipelineTest()
    passed, failed = await tester.run_all()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
