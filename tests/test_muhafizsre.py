# ═══════════════════════════════════════════════════════════════════════════════
# MuhafizSRE: Legacy Integration Test Suite (Updated for)
# ═══════════════════════════════════════════════════════════════════════════════
# Validates the core components of the Muhafiz pipeline:
#   - Agent initialization (correct model, tools, and configuration)
#   - MCP server tool execution (mock telemetry data)
#   - Gateway store (hash chain integrity)
#   - Agent skills (remediation actions)
#   - Recovery verification (system health check)
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import sys
import os
import json
import tempfile

import pytest

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── Test 1: MCP Server Tools ────────────────────────────────────────────────
def test_mcp_cloud_logging():
    """Verify get_cloud_logging_traces returns structured log data."""
    from shared.mcp_server.server import get_cloud_logging_traces
    result = get_cloud_logging_traces("auth-service")
    data = json.loads(result)
    assert "service_id" in data, "Missing service_id in response"
    assert data["service_id"] == "auth-service"
    assert "entries" in data, "Missing entries"
    assert len(data["entries"]) > 0, "No log entries returned"
    print("✅ test_mcp_cloud_logging passed")


def test_mcp_deployments():
    """Verify get_github_deployments returns deployment data."""
    from shared.mcp_server.server import get_github_deployments
    result = get_github_deployments("auth-service")
    data = json.loads(result)
    assert "deployments" in data or "repo" in data, "Missing deployment data"
    print("✅ test_mcp_deployments passed")


def test_mcp_metrics():
    """Verify get_system_metrics returns metric data."""
    from shared.mcp_server.server import get_system_metrics
    result = get_system_metrics("auth-service")
    data = json.loads(result)
    assert "resource" in data, "Missing resource in metrics"
    assert data["resource"] == "auth-service"
    print("✅ test_mcp_metrics passed")


def test_mcp_unknown_service():
    """Verify MCP handles unknown services gracefully."""
    from shared.mcp_server.server import get_cloud_logging_traces
    result = get_cloud_logging_traces("nonexistent-service")
    data = json.loads(result)
    # Should still return valid JSON, possibly with empty entries
    assert isinstance(data, dict), "Should return valid JSON dict"
    print("✅ test_mcp_unknown_service passed")


# ─── Test 2: Agent Skills (async adapters) ─────────────────────────────
def test_rollback_skill():
    """Verify rollback skill returns success."""
    from shared.skills import rollback_service_revision
    result = asyncio.run(rollback_service_revision("auth-service", "rev-v1"))
    assert result["status"] == "success", f"Expected success, got {result['status']}"
    assert result["service"] == "auth-service"
    assert "execution_id" in result
    print("✅ test_rollback_skill passed")


def test_rate_limit_skill():
    """Verify rate limiting skill returns success."""
    from shared.skills import apply_rate_limit
    result = asyncio.run(apply_rate_limit("auth-service", 100, 300))
    assert result["status"] == "success", f"Expected success, got {result['status']}"
    print("✅ test_rate_limit_skill passed")


def test_scale_skill():
    """Verify scaling skill returns success."""
    from shared.skills import scale_service
    result = asyncio.run(scale_service("auth-service", 3))
    assert result["status"] == "success", f"Expected success, got {result['status']}"
    print("✅ test_scale_skill passed")


def test_restart_skill():
    """Verify restart skill returns success."""
    from shared.skills import restart_service
    result = asyncio.run(restart_service("auth-service", graceful=True))
    assert result["status"] == "success", f"Expected success, got {result['status']}"
    print("✅ test_restart_skill passed")


def test_cache_flush_skill():
    """Verify cache flush skill returns success."""
    from shared.skills import flush_cache
    result = asyncio.run(flush_cache("auth-service", cache_type="all"))
    assert result["status"] == "success", f"Expected success, got {result['status']}"
    print("✅ test_cache_flush_skill passed")


def test_credential_rotation_skill():
    """Verify credential rotation skill returns success."""
    from shared.skills import rotate_credentials
    result = asyncio.run(rotate_credentials("auth-service", "api_key"))
    assert result["status"] == "success", f"Expected success, got {result['status']}"
    print("✅ test_credential_rotation_skill passed")


# ─── Test 3: Recovery Verification ───────────────────────────────────────────
def test_recovery_verification():
    """Verify the recovery verifier runs successfully."""
    from shared.recovery_verifier import verify_recovery
    result = asyncio.run(verify_recovery("auth-service"))
    assert isinstance(result, dict), "Should return a dict"
    assert "status" in result, "Missing status field"
    assert result["status"] == "RECOVERED", f"Expected RECOVERED, got {result['status']}"
    assert result["recovery_score"] == 1.0, "Score should be 1.0 in simulation"
    print("✅ test_recovery_verification passed")


# ─── Test 4: Gateway Store (Hash Chain) ──────────────────────────────────────
@pytest.mark.asyncio
async def test_store_hash_chain():
    """Verify the IncidentStore maintains hash chain integrity."""
    from gateway.store import IncidentStore
    from gateway.models import Alert, Severity

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_legacy.db")
        store = IncidentStore(db_path)
        await store.initialize()

        alert = Alert(
            severity=Severity.P1,
            service_id="auth-service",
            summary="Test incident for chain verification",
            error_message="Test error",
        )

        _inc = await store.create_incident(
            incident_id="INC-LEGACY-001",
            alert=alert,
            scenario_id="test",
        )

        run = await store.claim_pipeline_run(
            incident_id="INC-LEGACY-001",
            phase="phase1",
            revision=1,
            start_stage="triage",
            input_data={"test": True},
        )

        # Append events
        e1 = await store.append_event(
            incident_id="INC-LEGACY-001",
            run_id=run["run_id"],
            actor="test",
            actor_role="tester",
            event_type="test_event_1",
            summary="First event",
            payload={"action": "rollback"},
        )

        e2 = await store.append_event(
            incident_id="INC-LEGACY-001",
            run_id=run["run_id"],
            actor="test",
            actor_role="tester",
            event_type="test_event_2",
            summary="Second event",
            payload={"action": "scale"},
        )

        assert isinstance(e1["event_hash"], str) and len(e1["event_hash"]) == 64
        assert e1["event_hash"] != e2["event_hash"]

        # Verify chain integrity
        is_valid = await store.verify_incident_chain("INC-LEGACY-001")
        assert is_valid, "Chain integrity check failed"

        print("✅ test_store_hash_chain passed")


# ─── Test 5: Agent Definitions ───────────────────────────────────────────────
def test_agent_definitions():
    """Verify all 5 agents can be imported and have correct structure."""
    from agents.nigehban import nigehban
    from agents.muhaqqiq import muhaqqiq
    from agents.mudabbir import mudabbir
    from agents.muhtasib import muhtasib
    from agents.aamil import aamil
    
    agents = [nigehban, muhaqqiq, mudabbir, muhtasib, aamil]
    names = ["nigehban", "muhaqqiq", "mudabbir", "muhtasib", "aamil"]
    
    for agent, expected_name in zip(agents, names):
        assert agent.name == expected_name, f"Expected name '{expected_name}', got '{agent.name}'"
        assert agent.model is not None, f"Agent {expected_name} has no model"
        assert agent.instruction is not None, f"Agent {expected_name} has no instruction"
    
    print("✅ test_agent_definitions passed")


def test_agent_registry():
    """Verify the AGENT_REGISTRY contains all 5 agents."""
    from agents.agent import AGENT_REGISTRY
    
    assert len(AGENT_REGISTRY) == 5, f"Expected 5 agents, got {len(AGENT_REGISTRY)}"
    
    expected_agents = {"nigehban", "muhaqqiq", "mudabbir", "muhtasib", "aamil"}
    actual_agents = set(AGENT_REGISTRY.keys())
    assert actual_agents == expected_agents, f"Agent mismatch: {actual_agents}"
    
    # Verify each agent has a name attribute
    for name, agent in AGENT_REGISTRY.items():
        assert hasattr(agent, 'name'), f"Agent {name} missing 'name' attribute"
        assert agent.name == name, f"Agent name mismatch: {agent.name} != {name}"
    
    print("✅ test_agent_registry passed")


# ─── Test 6: Edge Cases & Validation ─────────────────────────────────────────
def test_skill_empty_service_name():
    """Verify skills reject empty service names with proper error messages."""
    from shared.skills import rollback_service_revision
    
    result = asyncio.run(rollback_service_revision(service_name="", target_revision="v1"))
    assert result["status"] == "error", "Empty service_name should return error"
    print("✅ test_skill_empty_service_name passed")


@pytest.mark.asyncio
async def test_store_chain_tamper_detection():
    """Verify the store detects tampering when an event is modified."""
    import aiosqlite
    from gateway.store import IncidentStore
    from gateway.models import Alert, Severity

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_tamper.db")
        store = IncidentStore(db_path)
        await store.initialize()

        alert = Alert(
            severity=Severity.P2,
            service_id="payment-gateway",
            summary="Tamper test",
            error_message="Test",
        )

        await store.create_incident(
            incident_id="INC-TAMPER-001",
            alert=alert,
            scenario_id="test",
        )

        run = await store.claim_pipeline_run(
            incident_id="INC-TAMPER-001",
            phase="phase1",
            revision=1,
            start_stage="triage",
            input_data={},
        )

        await store.append_event(
            incident_id="INC-TAMPER-001",
            run_id=run["run_id"],
            actor="test",
            actor_role="tester",
            event_type="event_1",
            summary="First",
            payload={},
        )
        await store.append_event(
            incident_id="INC-TAMPER-001",
            run_id=run["run_id"],
            actor="test",
            actor_role="tester",
            event_type="event_2",
            summary="Second",
            payload={},
        )

        assert await store.verify_incident_chain("INC-TAMPER-001"), "Chain should be valid before tampering"

        # Tamper with a record directly in SQLite
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE events SET summary='TAMPERED' WHERE sequence=1 AND incident_id='INC-TAMPER-001'"
            )
            await db.commit()

        # Chain verification should now FAIL
        is_valid = await store.verify_incident_chain("INC-TAMPER-001")
        assert not is_valid, "Chain should detect tampering"
        print("✅ test_store_chain_tamper_detection passed")


def test_gateway_health_endpoint():
    """Verify the gateway /health endpoint returns correct structure."""
    from fastapi.testclient import TestClient
    from gateway.app import app

    # Ensure test mode is enabled for lifespan validation
    os.environ["MUHAFIZ_TEST_MODE"] = "true"
    os.environ.setdefault(
        "MUHAFIZ_APPROVAL_SECRET",
        "test-secret-that-is-at-least-32-characters-long-for-testing",
    )

    # The TestClient context manager triggers the lifespan startup event
    with TestClient(app) as client:
        response = client.get("/health")

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert data["status"] == "healthy", f"Expected 'healthy', got {data['status']}"
        assert "service" in data, "Response should include 'service'"
        assert "timestamp" in data, "Response should include 'timestamp'"
    print("✅ test_gateway_health_endpoint passed")
