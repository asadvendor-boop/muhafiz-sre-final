"""
tests/test_sandbox_smoke.py – Sandbox Smoke Test for MuhafizSRE
===================================================================

Performs a live-loop validation using a programmatically spun-up instance
of the real victim service (auth-service) FastAPI application:
    1. Spin up victim app on 127.0.0.1:9009
    2. Inject fault → health check returns HTTP 503 (unhealthy)
    3. Run real rollback_service_revision skill → recovery endpoint hit
    4. Health check returns HTTP 200 (healthy/recovered)
"""

import asyncio
import threading
from unittest.mock import patch

import httpx
import pytest
import uvicorn

from evaluation.victim.app import app as victim_app
from shared.skills import rollback_service_revision


class UvicornTestServer(uvicorn.Server):
    """Programmatic Uvicorn Server to run in a background thread for smoke testing."""
    def __init__(self, app, host="127.0.0.1", port=9009):
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        super().__init__(config)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


@pytest.mark.asyncio
async def test_sandbox_smoke_recovery(monkeypatch):
    # Set sandbox execution mode
    monkeypatch.setenv("MUHAFIZ_EXECUTION_MODE", "sandbox")
    monkeypatch.setenv("MUHAFIZ_SANDBOX_TARGET_SERVICE", "auth-service")

    # 1. Start the real victim service in a background thread
    port = 9009
    server = UvicornTestServer(victim_app, host="127.0.0.1", port=port)
    server.start()

    # Wait a moment for server to start
    await asyncio.sleep(0.5)

    victim_url = f"http://127.0.0.1:{port}"

    try:
        async with httpx.AsyncClient() as client:
            # 2. Inject fault to make victim unhealthy
            inject_resp = await client.post(f"{victim_url}/inject-fault?error_rate=1.0&latency_ms=0")
            assert inject_resp.status_code == 200

            # Verify it is indeed unhealthy (HTTP 503)
            health_resp = await client.get(f"{victim_url}/health")
            assert health_resp.status_code == 503
            assert health_resp.json()["status"] == "unhealthy"

            # 3. Call the real rollback skill to trigger recovery
            result = await rollback_service_revision(
                service_name="auth-service",
                target_revision="rev-v2",
                victim_url=victim_url
            )
            assert result["status"] == "success"
            assert result["adapter"] == "sandbox"
            assert result["is_real_mutation"] is True
            assert result["detail"]["recovery_verified"] is True
            assert result["detail"]["before_state"]["healthy"] is False
            assert result["detail"]["after_state"]["healthy"] is True

            # 4. Verify victim service is recovered (HTTP 200)
            health_resp2 = await client.get(f"{victim_url}/health")
            assert health_resp2.status_code == 200
            assert health_resp2.json()["status"] == "healthy"
    finally:
        # 5. Stop the background server
        server.stop()


@pytest.mark.asyncio
async def test_sandbox_smoke_full_flow(tmp_path, monkeypatch):
    # 1. Start the real victim service in a background thread
    port = 9009
    server = UvicornTestServer(victim_app, host="127.0.0.1", port=port)
    server.start()

    # Wait a moment for server to start
    await asyncio.sleep(0.5)

    victim_url = f"http://127.0.0.1:{port}"

    # Configure env vars for this test
    monkeypatch.setenv("MUHAFIZ_APPROVAL_SECRET", "test-secret-key-at-least-32-characters-long!!")
    monkeypatch.setenv("MUHAFIZ_TEST_MODE", "true")
    monkeypatch.setenv("MUHAFIZ_DB_PATH", str(tmp_path / "test_gateway.db"))
    monkeypatch.setenv("VICTIM_SERVICE_URL", victim_url)
    monkeypatch.setenv("MUHAFIZ_EXECUTION_MODE", "sandbox")
    monkeypatch.setenv("MUHAFIZ_SANDBOX_TARGET_SERVICE", "auth-service")

    from httpx import AsyncClient, ASGITransport
    from gateway.security import Settings, ApprovalTokenManager
    from gateway.store import IncidentStore
    from shared.dependencies import init_dependencies

    # Initialize gateway dependencies
    settings = Settings.from_env()
    settings.test_mode = True
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        import secrets as _s
        settings.approval_secret = _s.token_hex(32)

    token_manager = ApprovalTokenManager(settings.approval_secret)
    store = IncidentStore(settings.db_path)
    await store.initialize()
    init_dependencies(store=store, token_manager=token_manager, settings=settings)

    # Let's define the agent mock side effects
    async def mock_run_single_agent(agent_name: str, state: dict, message: str, thinking_level: str | None = None) -> dict:
        from shared.dependencies import get_store
        store = get_store()
        incident_id = state.get("incident_id", "")

        if agent_name == "nigehban":
            await store.update_incident(incident_id, status="ANALYZING")
            return {
                **state,
                "triage_result": {
                    "is_actionable": True,
                    "severity_confirmed": "P1",
                    "confidence": 0.95,
                },
            }
        elif agent_name == "muhaqqiq":
            await store.update_incident(incident_id, status="PLANNING")
            return {
                **state,
                "investigation_result": {
                    "root_cause_code": "BAD_DEPLOYMENT",
                    "root_cause_summary": "Deployment rev-v1 introduced regression",
                    "evidence": [{"source": "investigation", "data": "deployment rev-v1 introduced regression", "trust": "direct"}],
                    "tool_calls_made": ["get_cloud_logging_traces"],
                    "confidence": 0.9,
                    "affected_components": ["auth-service"],
                    "contributing_factors": [],
                },
            }
        elif agent_name == "mudabbir":
            await store.update_incident(incident_id, status="REVIEWING")
            return {
                **state,
                "plan": {
                    "plan_id": f"PLAN-{incident_id}",
                    "revision": state.get("plan_revision", 1),
                    "actions": [
                        {
                            "action_id": "act-001",
                            "skill": "rollback_service_revision",
                            "target": "auth-service",
                            "arguments": {"service_name": "auth-service", "target_revision": "rev-v2"},
                            "order": 1,
                            "on_failure": "STOP",
                        }
                    ],
                },
                "plan_event_hash": "mock-plan-hash",
            }
        elif agent_name == "muhtasib":
            return {
                **state,
                "verdict": {
                    "decision": "APPROVED_REQUIRES_HUMAN",
                    "reasoning": "Plan is safe: rollback only.",
                    "risk_level": "LOW",
                    "first_pass_commit": True,
                    "retry_used": False,
                },
                "verdict_event_hash": f"mock-verdict-hash-{id(state)}",
            }
        elif agent_name == "aamil":
            from agents.aamil import execute_approved_actions
            from unittest.mock import MagicMock
            # Execute real Aamil action executor which executes real rollback skill on victim
            tool_context = MagicMock()
            tool_context.state = state
            result = await execute_approved_actions(tool_context)

            # Real Aamil returns a dictionary with receipts
            new_state = {
                **state,
                "execution_receipts": result.get("receipts", {}),
                "all_actions_succeeded": result.get("all_succeeded", False),
                "reconciliation": {
                    "status": "all_succeeded" if result.get("all_succeeded", False) else "partial",
                    "succeeded": result.get("actions_executed", 0),
                    "total": result.get("actions_total", 0),
                }
            }
            return new_state
        return state

    try:
        async with httpx.AsyncClient() as client:
            # Inject fault to make victim unhealthy
            inject_resp = await client.post(f"{victim_url}/inject-fault?error_rate=1.0&latency_ms=0")
            assert inject_resp.status_code == 200

            # Verify it is unhealthy
            health_resp = await client.get(f"{victim_url}/health")
            assert health_resp.status_code == 503

        # Patch agent runner with our mock side effect
        with patch("gateway.app._run_single_agent", side_effect=mock_run_single_agent):
            from gateway.app import app as gateway_app
            transport = ASGITransport(app=gateway_app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://test") as gateway_client:
                # 1. Trigger incident
                alert_payload = {
                    "alert": {
                        "alert_type": "error_rate",
                        "service_id": "auth-service",
                        "severity": "P1",
                        "summary": "Error rate spike to 100% on auth-service",
                        "error_message": "Connection refused",
                        "timestamp": "2026-06-21T12:00:00Z",
                    },
                    "scenario_id": "bad_deployment",
                }
                resp = await gateway_client.post("/api/incidents", json=alert_payload)
                assert resp.status_code == 201
                incident_id = resp.json()["incident_id"]

                # 2. Wait for Phase 1 (AWAITING_APPROVAL status)
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    resp = await gateway_client.get(f"/api/incidents/{incident_id}")
                    if resp.json().get("status") == "AWAITING_APPROVAL":
                        break
                else:
                    pytest.fail("Gateway did not reach AWAITING_APPROVAL state")

                # 3. Retrieve contract to get approval token and claims
                resp = await gateway_client.get(f"/api/incidents/{incident_id}/contract")
                contract_data = resp.json()
                token = contract_data["approval_token"]
                contract_id = contract_data["contract"]["contract_id"]
                revision = contract_data["contract"]["revision"]

                # 4. POST approval decision
                decision_payload = {
                    "action": "APPROVE",
                    "contract_id": contract_id,
                    "revision": revision,
                    "approval_token": token,
                }
                resp = await gateway_client.post(f"/api/incidents/{incident_id}/decisions", json=decision_payload)
                assert resp.status_code == 200

                # 5. Wait for Phase 2 execution to complete (RESOLVED status)
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    resp = await gateway_client.get(f"/api/incidents/{incident_id}")
                    if resp.json().get("status") == "RESOLVED":
                        break
                else:
                    pytest.fail("Gateway did not reach RESOLVED state")

                # 6. Verify victim is recovered (HTTP 200)
                async with httpx.AsyncClient() as client:
                    health_resp2 = await client.get(f"{victim_url}/health")
                    assert health_resp2.status_code == 200
                    assert health_resp2.json()["status"] == "healthy"

    finally:
        server.stop()
