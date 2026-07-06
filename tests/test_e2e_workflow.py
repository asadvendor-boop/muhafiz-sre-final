"""
tests/test_e2e_workflow.py – End-to-end workflow integration test
=================================================================

Validates the complete incident lifecycle through the gateway API:

    alert → triage → investigation → planning → safety review
    → contract issuance → GET /contract (token) → POST /decisions (APPROVE)
    → Aamil execution → recovery verification → incident seal

Agents are stubbed at the _run_single_agent boundary so NO Gemini
calls are made.  The test exercises:
    - Gateway state machine transitions
    - Contract issuance with immutable claims_json
    - Token round-trip: GET /contract → POST /decisions
    - Phase 2 execution (Aamil) + recovery verification
    - Hash-chain seal event
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch, tmp_path):
    """Ensure deterministic environment for each test run."""
    monkeypatch.setenv("MUHAFIZ_APPROVAL_SECRET", "test-secret-key-at-least-32-characters-long!!")
    monkeypatch.setenv("MUHAFIZ_TEST_MODE", "true")
    monkeypatch.setenv("MUHAFIZ_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("VICTIM_SERVICE_URL", "http://localhost:9000")


# ────────────────────────────────────────────────────────────────────────────
# Agent stubs — simulate agent outputs via session state mutations
# ────────────────────────────────────────────────────────────────────────────

_AGENT_SIDE_EFFECTS = {
    "nigehban": lambda state, _msg: {
        **state,
        "triage_result": {
            "is_actionable": True,
            "severity_confirmed": "P1",
            "confidence": 0.95,
        },
    },
    "muhaqqiq": lambda state, _msg: {
        **state,
        "investigation_result": {
            "root_cause_code": "BAD_DEPLOYMENT",
            "evidence": [{"source": "investigation", "data": "deployment rev-2024-06-20 introduced regression", "trust": "direct"}],
            "tool_calls_made": ["get_cloud_logging_traces", "get_github_deployments"],
            "confidence": 0.9,
            "affected_components": ["auth-service"],
            "root_cause_summary": "Bad deployment rev-2024-06-20 caused JWT validation failures",
            "contributing_factors": ["missing JWT key rotation"],
        },
    },
    "mudabbir": lambda state, _msg: {
        **state,
        "plan": {
            "plan_id": f"PLAN-{state.get('incident_id', 'TEST')}",
            "revision": state.get("plan_revision", 1),
            "actions": [
                {
                    "skill": "rollback_service_revision",
                    "service_id": "auth-service",
                    "target_revision": "rev-2024-06-19",
                    "order": 1,
                    "failure_policy": "STOP",
                }
            ],
        },
        "plan_event_hash": "stub-plan-hash",
    },
    "muhtasib": lambda state, _msg: {
        **state,
        "verdict": {
            "decision": "APPROVED_REQUIRES_HUMAN",
            "reasoning": "Plan is safe: single rollback with known-good revision.",
            "risk_level": "LOW",
            "first_pass_commit": True,
            "retry_used": False,
        },
        "verdict_event_hash": f"stub-verdict-hash-{id(state)}",
    },
    "aamil": lambda state, _msg: {
        **state,
        "execution_receipts": {
            "rollback_service_revision": {
                "status": "success",
                "result": {"message": "Rolled back to rev-2024-06-19"},
            },
        },
        "all_actions_succeeded": True,
        "reconciliation": {
            "status": "all_succeeded",
            "succeeded": 1,
            "total": 1,
        },
    },
}


async def _mock_run_single_agent(agent_name: str, state: dict, message: str, thinking_level: str | None = None) -> dict:
    """Stub that returns pre-canned state mutations per agent.

    Also performs the store state transitions that real agents would do,
    so the pipeline's compare-and-set transitions succeed.
    """
    from shared.dependencies import get_store

    fn = _AGENT_SIDE_EFFECTS.get(agent_name)
    if fn is None:
        return state
    result = fn(state, message)

    # Replicate the status transitions real agents perform
    incident_id = state.get("incident_id", "")
    store = get_store()
    if agent_name == "nigehban":
        # Nigehban: DETECTED → ANALYZING
        await store.update_incident(incident_id, status="ANALYZING")
    elif agent_name == "muhaqqiq":
        # Muhaqqiq: ANALYZING → PLANNING
        await store.update_incident(incident_id, status="PLANNING")
    elif agent_name == "mudabbir":
        # Mudabbir: PLANNING → REVIEWING
        await store.update_incident(incident_id, status="REVIEWING")

    return result


_mock_recovery = {
    "status": "RECOVERED",
    "recovery_score": 1.0,
    "checks": {"health_endpoint": True, "error_rate": 0.0},
    "verified_at": "2026-06-21T12:00:00+00:00",
}


# ────────────────────────────────────────────────────────────────────────────
# The test
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_workflow_alert_to_seal():
    """
    Complete incident lifecycle:
        POST /api/incidents (alert)
        → Phase 1 background (agents stubbed)
        → GET /api/incidents/{id}/contract (obtain token)
        → POST /api/incidents/{id}/decisions (APPROVE with token)
        → Phase 2 background (Aamil + recovery, stubbed)
        → GET /api/incidents/{id} (verify RESOLVED + sealed)
        → GET /api/incidents/{id}/chain/verify (hash chain valid)
    """
    from httpx import AsyncClient, ASGITransport
    from gateway.security import Settings, ApprovalTokenManager
    from gateway.store import IncidentStore
    from shared.dependencies import init_dependencies

    # ── Manual DI initialization (lifespan substitute) ───────────────
    settings = Settings.from_env()
    # Force test mode to avoid secret length validation
    settings.test_mode = True
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        import secrets as _s
        settings.approval_secret = _s.token_hex(32)

    token_manager = ApprovalTokenManager(settings.approval_secret)
    store = IncidentStore(settings.db_path)
    await store.initialize()
    init_dependencies(store=store, token_manager=token_manager, settings=settings)

    # Initialize PipelineSupervisor (mirrors lifespan)
    from gateway.pipeline_supervisor import PipelineSupervisor
    from gateway.app import app
    app.state.pipeline_supervisor = PipelineSupervisor(store=store, max_concurrent=2)

    # Patch agents and recovery
    with (
        patch("gateway.app._run_single_agent", side_effect=_mock_run_single_agent),
        patch(
            "shared.recovery_verifier.verify_recovery",
            new_callable=AsyncMock,
            return_value=_mock_recovery,
        ),
    ):
        from gateway.app import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # ── Step 1: Create incident ──────────────────────────────────
            alert_payload = {
                "alert": {
                    "alert_type": "error_rate",
                    "service_id": "auth-service",
                    "severity": "P1",
                    "summary": "Error rate spike to 45% after deployment",
                    "error_message": "500 Internal Server Error",
                    "timestamp": "2026-06-21T12:00:00Z",
                    "metric_value": 45.0,
                    "threshold": 5.0,
                },
                "scenario_id": "bad_deployment",
            }
            resp = await client.post("/api/incidents", json=alert_payload)
            assert resp.status_code == 201, f"Create failed: {resp.text}"
            data = resp.json()
            incident_id = data["incident_id"]
            assert incident_id.startswith("INC-")

            # ── Step 2: Wait for Phase 1 pipeline to complete ────────────
            # Phase 1 runs as an asyncio.create_task; give it time to finish
            for _ in range(50):
                await asyncio.sleep(0.1)
                resp = await client.get(f"/api/incidents/{incident_id}")
                inc = resp.json()
                if inc.get("status") in (
                    "AWAITING_APPROVAL", "FALSE_ALARM", "BLOCKED", "ESCALATED",
                ):
                    break
            else:
                pytest.fail(
                    f"Phase 1 did not complete in time. Status: {inc.get('status')}"
                )

            assert inc["status"] == "AWAITING_APPROVAL", (
                f"Expected AWAITING_APPROVAL, got {inc['status']}"
            )

            # ── Step 3: GET /contract — obtain approval token ────────────
            resp = await client.get(f"/api/incidents/{incident_id}/contract")
            assert resp.status_code == 200, f"Contract fetch failed: {resp.text}"
            contract_data = resp.json()
            assert contract_data["contract"] is not None, "No active contract"
            contract = contract_data["contract"]
            approval_token = contract_data["approval_token"]
            assert approval_token, "No approval token returned"
            assert contract["status"] == "ISSUED"

            contract_id = contract["contract_id"]
            revision = contract["revision"]

            # ── Step 4: POST /decisions — APPROVE with that exact token ──
            decision_payload = {
                "contract_id": contract_id,
                "revision": revision,
                "action": "APPROVE",
                "operator_label": "e2e-test-operator",
                "approval_token": approval_token,
            }
            resp = await client.post(
                f"/api/incidents/{incident_id}/decisions",
                json=decision_payload,
            )
            assert resp.status_code == 200, f"Decision failed: {resp.text}"
            decision = resp.json()
            assert decision["status"] == "approved"
            assert decision["event_hash"], "Missing event hash"

            # ── Step 5: Wait for Phase 2 (execution + recovery + seal) ───
            for _ in range(50):
                await asyncio.sleep(0.1)
                resp = await client.get(f"/api/incidents/{incident_id}")
                inc = resp.json()
                if inc.get("status") in (
                    "RESOLVED", "RECOVERY_FAILED", "EXECUTION_FAILED", "DEGRADED",
                ):
                    break
            else:
                pytest.fail(
                    f"Phase 2 did not complete in time. Status: {inc.get('status')}"
                )

            assert inc["status"] == "RESOLVED", (
                f"Expected RESOLVED, got {inc['status']}"
            )

            # ── Step 6: Verify hash chain integrity ──────────────────────
            resp = await client.get(
                f"/api/incidents/{incident_id}/chain/verify"
            )
            assert resp.status_code == 200
            chain = resp.json()
            assert chain["chain_valid"] is True, "Hash chain is broken!"

            # ── Step 7: Verify audit proof structure ─────────────────────
            resp = await client.get(f"/api/incidents/{incident_id}/audit")
            assert resp.status_code == 200
            audit = resp.json()
            assert audit["chain_valid"] is True, "Audit chain invalid"
            assert audit["record_count"] > 0, "No events recorded"
            assert audit["final_event_hash"], "Missing final event hash"

            # Query store directly for full event verification
            events = await store.get_events(incident_id)
            event_types = [e["event_type"] for e in events]

            assert "incident_created" in event_types, "Missing incident_created"
            assert "contract_issued" in event_types, "Missing contract_issued"
            assert "human_approved" in event_types, "Missing human_approved"
            assert "recovery_verified" in event_types, "Missing recovery_verified"
            assert "outcome" in event_types, "Missing outcome"
            assert "seal" in event_types, "Missing seal event"

            # Verify seal payload
            seal_event = next(e for e in events if e["event_type"] == "seal")
            seal_payload = json.loads(seal_event.get("payload_json", "{}"))
            assert seal_payload.get("final_status") == "RESOLVED"
            assert seal_payload.get("pre_seal_head_hash"), "Missing pre_seal hash"

            # ── Step 8: Verify room messages exist ───────────────────────
            resp = await client.get(f"/api/incidents/{incident_id}/room")
            assert resp.status_code == 200
            room = resp.json()
            assert room["count"] > 0, "No room messages recorded"
