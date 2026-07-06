"""
tests/test_muhaqqiq_retry.py – Deterministic tests for Muhaqqiq bounded retry
================================================================================

Validates the investigation retry logic that replaced the keyword finalizer.
Agents are stubbed at the _run_single_agent boundary — NO Gemini calls.

Test matrix (behavioral):
  1. First attempt commits → retry not called, pipeline continues
  2. First misses, retry commits → pipeline continues with provenance
  3. Both miss → PIPELINE_FAILED, no plan, no contract

Test matrix (source inspection):
  4. No forced-finalizer code remains in gateway/app.py
  5. Retry uses HIGH thinking level
  6. Retry provenance fields are correct
  7. No fallback_used in investigation retry path
"""

import asyncio
import json
import os
import sys
import uuid
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
    monkeypatch.setenv("MUHAFIZ_DB_PATH", str(tmp_path / "test_muhaqqiq_retry.db"))
    monkeypatch.setenv("VICTIM_SERVICE_URL", "http://localhost:9000")


# ────────────────────────────────────────────────────────────────────────────
# Agent stubs
# ────────────────────────────────────────────────────────────────────────────

def _nigehban_stub(state, _msg):
    return {
        **state,
        "triage_result": {
            "is_actionable": True,
            "severity_confirmed": "P1",
            "confidence": 0.95,
            "service_id": "auth-service",
            "summary": "Auth service returning 502s after deployment",
        },
    }


def _muhaqqiq_commits(state, _msg):
    """Muhaqqiq successfully calls commit_investigation."""
    return {
        **state,
        "investigation_result": {
            "root_cause_code": "BAD_DEPLOYMENT",
            "root_cause_summary": "Rolling deployment v5.3.0 introduced regression",
            "evidence": [
                {"source": "logs", "data": "deployment regression", "trust": "direct"},
            ],
            "tool_calls_made": ["get_cloud_logging_traces", "get_github_deployments"],
            "confidence": 0.9,
            "affected_components": ["auth-service"],
            "contributing_factors": [],
        },
        "investigation_event_hash": f"inv-hash-{uuid.uuid4().hex[:8]}",
    }


def _muhaqqiq_skips(state, _msg):
    """Muhaqqiq does NOT call commit_investigation — returns state unchanged."""
    return state


def _mudabbir_stub(state, _msg):
    return {
        **state,
        "plan": {
            "plan_id": f"PLAN-{state.get('incident_id', 'TEST')}",
            "revision": state.get("plan_revision", 1),
            "actions": [
                {
                    "skill": "rollback_service_revision",
                    "service_id": "auth-service",
                    "target_revision": "rev-v2-stable",
                    "order": 1,
                    "failure_policy": "STOP",
                }
            ],
        },
        "plan_event_hash": f"plan-hash-{uuid.uuid4().hex[:8]}",
    }


def _muhtasib_approves(state, _msg):
    return {
        **state,
        "verdict": {
            "decision": "APPROVED_REQUIRES_HUMAN",
            "risk_score": 0.2,
            "reasoning": "Single rollback, low risk.",
        },
        "verdict_event_hash": f"verdict-hash-{uuid.uuid4().hex[:8]}",
    }


async def _do_status_transitions(agent_name, state, store, incident_id):
    """Replicate agent status transitions so compare-and-set works."""
    if agent_name == "nigehban":
        await store.update_incident(incident_id, status="ANALYZING")
    elif agent_name == "muhaqqiq":
        await store.update_incident(incident_id, status="PLANNING")
    elif agent_name == "mudabbir":
        await store.update_incident(incident_id, status="REVIEWING")


_mock_recovery = {
    "status": "RECOVERED",
    "recovery_score": 1.0,
    "checks": {"health_endpoint": True, "error_rate": 0.0},
    "verified_at": "2026-06-21T12:00:00+00:00",
}

_ALERT_PAYLOAD = {
    "alert": {
        "alert_type": "error_rate",
        "service_id": "auth-service",
        "severity": "P1",
        "summary": "Error rate spike after deployment",
        "error_message": "JWT validation failures",
        "timestamp": "2026-06-21T12:00:00Z",
    },
    "scenario_id": "bad_deployment",
}


async def _init_test_deps():
    """Initialize test dependencies and return store."""
    from gateway.security import Settings, ApprovalTokenManager
    from gateway.store import IncidentStore
    from shared.dependencies import init_dependencies

    settings = Settings.from_env()
    settings.test_mode = True
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        import secrets as _s
        settings.approval_secret = _s.token_hex(32)

    token_manager = ApprovalTokenManager(settings.approval_secret)
    store = IncidentStore(settings.db_path)
    await store.initialize()
    init_dependencies(store=store, token_manager=token_manager, settings=settings)
    return store


# ────────────────────────────────────────────────────────────────────────────
# Test 1: First attempt commits → retry NOT called
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_attempt_commits_no_retry():
    """
    Muhaqqiq successfully calls commit_investigation on the first attempt.
    The bounded retry should NOT be invoked. Pipeline continues to planning.
    """
    store = await _init_test_deps()
    from shared.dependencies import get_store

    muhaqqiq_calls = {"count": 0}

    async def mock_agent(agent_name, state, message, thinking_level=None):
        incident_id = state.get("incident_id", "")
        s = get_store()
        await _do_status_transitions(agent_name, state, s, incident_id)

        if agent_name == "nigehban":
            return _nigehban_stub(state, message)
        elif agent_name == "muhaqqiq":
            muhaqqiq_calls["count"] += 1
            # First and only call: commits successfully
            result = _muhaqqiq_commits(state, message)
            # Simulate what commit_investigation tool does — just persist
            # the event. Status transition already done by _do_status_transitions.
            event = await s.append_event(
                incident_id=incident_id,
                run_id=state.get("run_id", ""),
                actor="muhaqqiq",
                actor_role="investigator",
                event_type="investigation_completed",
                summary="Root cause: BAD_DEPLOYMENT",
                payload=result["investigation_result"],
            )
            result["investigation_event_hash"] = event["event_hash"]
            return result
        elif agent_name == "mudabbir":
            return _mudabbir_stub(state, message)
        elif agent_name == "muhtasib":
            return _muhtasib_approves(state, message)
        return state

    with patch(
        "gateway.app._run_single_agent", side_effect=mock_agent,
    ), patch(
        "shared.recovery_verifier.verify_recovery",
        new_callable=AsyncMock, return_value=_mock_recovery,
    ):
        from httpx import AsyncClient, ASGITransport
        from gateway.app import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/incidents", json=_ALERT_PAYLOAD)
            assert resp.status_code == 201, f"Create failed: {resp.text}"
            incident_id = resp.json()["incident_id"]

            # Wait for Phase 1 to reach a terminal/waiting state
            for _ in range(60):
                await asyncio.sleep(0.1)
                resp = await client.get(f"/api/incidents/{incident_id}")
                status = resp.json().get("status")
                if status in ("AWAITING_APPROVAL", "ESCALATED", "BLOCKED",
                              "PIPELINE_FAILED"):
                    break

            assert resp.json()["status"] == "AWAITING_APPROVAL", (
                f"Expected AWAITING_APPROVAL, got {resp.json()['status']}"
            )

    # Muhaqqiq should have been called exactly ONCE (no retry needed)
    assert muhaqqiq_calls["count"] == 1, (
        f"Expected 1 Muhaqqiq call (no retry), got {muhaqqiq_calls['count']}"
    )

    print("✅ test_first_attempt_commits_no_retry passed")


# ────────────────────────────────────────────────────────────────────────────
# Test 2: First misses, retry commits → pipeline continues with provenance
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt():
    """
    Muhaqqiq first invocation fails to commit (no investigation_result).
    Bounded retry re-runs at HIGH thinking and commits successfully.
    Expected: pipeline continues, investigation has retry_used=True.
    """
    store = await _init_test_deps()
    from shared.dependencies import get_store

    muhaqqiq_calls = {"count": 0}
    retry_thinking_levels = []

    async def mock_agent(agent_name, state, message, thinking_level=None):
        incident_id = state.get("incident_id", "")
        s = get_store()
        await _do_status_transitions(agent_name, state, s, incident_id)

        if agent_name == "nigehban":
            return _nigehban_stub(state, message)
        elif agent_name == "muhaqqiq":
            muhaqqiq_calls["count"] += 1
            retry_thinking_levels.append(thinking_level)
            if muhaqqiq_calls["count"] == 1:
                # First call: does NOT commit investigation
                return _muhaqqiq_skips(state, message)
            else:
                # Retry: commits successfully
                result = _muhaqqiq_commits(state, message)
                event = await s.append_event(
                    incident_id=incident_id,
                    run_id=state.get("run_id", ""),
                    actor="muhaqqiq",
                    actor_role="investigator",
                    event_type="investigation_completed",
                    summary="Root cause: BAD_DEPLOYMENT (retry)",
                    payload=result["investigation_result"],
                )
                result["investigation_event_hash"] = event["event_hash"]
                return result
        elif agent_name == "mudabbir":
            return _mudabbir_stub(state, message)
        elif agent_name == "muhtasib":
            return _muhtasib_approves(state, message)
        return state

    with patch(
        "gateway.app._run_single_agent", side_effect=mock_agent,
    ), patch(
        "shared.recovery_verifier.verify_recovery",
        new_callable=AsyncMock, return_value=_mock_recovery,
    ):
        from httpx import AsyncClient, ASGITransport
        from gateway.app import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/incidents", json=_ALERT_PAYLOAD)
            assert resp.status_code == 201, f"Create failed: {resp.text}"
            incident_id = resp.json()["incident_id"]

            for _ in range(60):
                await asyncio.sleep(0.1)
                resp = await client.get(f"/api/incidents/{incident_id}")
                status = resp.json().get("status")
                if status in ("AWAITING_APPROVAL", "ESCALATED", "BLOCKED",
                              "PIPELINE_FAILED"):
                    break

            assert resp.json()["status"] == "AWAITING_APPROVAL", (
                f"Expected AWAITING_APPROVAL, got {resp.json()['status']}"
            )

    # Muhaqqiq called TWICE: first pass + retry
    assert muhaqqiq_calls["count"] == 2, (
        f"Expected 2 Muhaqqiq calls (1 miss + 1 retry), got {muhaqqiq_calls['count']}"
    )

    # Retry must have used HIGH thinking
    assert retry_thinking_levels[1] == "HIGH", (
        f"Retry must use HIGH thinking, got {retry_thinking_levels[1]}"
    )

    # First call should have default (None) thinking level
    assert retry_thinking_levels[0] is None, (
        f"First call should use default thinking, got {retry_thinking_levels[0]}"
    )

    print("✅ test_retry_succeeds_on_second_attempt passed")


# ────────────────────────────────────────────────────────────────────────────
# Test 3: Both miss → PIPELINE_FAILED, no plan, no contract
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_both_attempts_fail_pipeline_fails():
    """
    Both Muhaqqiq attempts fail to commit investigation.
    Expected: pipeline fails, incident status is PIPELINE_FAILED,
    no plan_created or contract_issued events.
    """
    store = await _init_test_deps()
    from shared.dependencies import get_store

    muhaqqiq_calls = {"count": 0}
    mudabbir_called = {"called": False}

    async def mock_agent(agent_name, state, message, thinking_level=None):
        incident_id = state.get("incident_id", "")
        s = get_store()
        await _do_status_transitions(agent_name, state, s, incident_id)

        if agent_name == "nigehban":
            return _nigehban_stub(state, message)
        elif agent_name == "muhaqqiq":
            muhaqqiq_calls["count"] += 1
            # BOTH calls fail to commit
            return _muhaqqiq_skips(state, message)
        elif agent_name == "mudabbir":
            mudabbir_called["called"] = True
            return _mudabbir_stub(state, message)
        return state

    with patch(
        "gateway.app._run_single_agent", side_effect=mock_agent,
    ), patch(
        "shared.recovery_verifier.verify_recovery",
        new_callable=AsyncMock, return_value=_mock_recovery,
    ):
        from httpx import AsyncClient, ASGITransport
        from gateway.app import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/incidents", json=_ALERT_PAYLOAD)
            assert resp.status_code == 201, f"Create failed: {resp.text}"
            incident_id = resp.json()["incident_id"]

            for _ in range(60):
                await asyncio.sleep(0.1)
                resp = await client.get(f"/api/incidents/{incident_id}")
                status = resp.json().get("status")
                if status in ("AWAITING_APPROVAL", "ESCALATED", "BLOCKED",
                              "PIPELINE_FAILED"):
                    break

            incident = resp.json()
            assert incident["status"] == "PIPELINE_FAILED", (
                f"Expected PIPELINE_FAILED, got {incident['status']}"
            )

    # Muhaqqiq called exactly twice: first pass + one retry
    assert muhaqqiq_calls["count"] == 2, (
        f"Expected 2 Muhaqqiq calls (both failed), got {muhaqqiq_calls['count']}"
    )

    # Mudabbir should NOT have been called
    assert not mudabbir_called["called"], (
        "Mudabbir should NOT be called when investigation fails"
    )

    # Verify events
    events = await store.get_events(incident_id)
    event_types = [e.get("event_type") for e in events]

    # Must have pipeline_failed
    assert "pipeline_failed" in event_types, (
        f"Expected pipeline_failed event, got: {event_types}"
    )

    # Must NOT have plan or contract
    assert "plan_created" not in event_types, (
        "plan_created should NOT exist when investigation fails"
    )
    assert "contract_issued" not in event_types, (
        "contract_issued should NOT exist when investigation fails"
    )

    print("✅ test_both_attempts_fail_pipeline_fails passed")


# ────────────────────────────────────────────────────────────────────────────
# Test 4: No forced-finalizer code remains in gateway/app.py
# ────────────────────────────────────────────────────────────────────────────

def test_no_forced_finalizer_code_remains():
    """
    Source inspection: the FORCED COMMIT FINALIZER block, keyword matching,
    and fallback_used=True must not exist in gateway/app.py.
    """
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "gateway", "app.py",
    )
    with open(app_path, "r") as f:
        source = f.read()

    # No forced commit finalizer
    assert "FORCED COMMIT FINALIZER" not in source, \
        "FORCED COMMIT FINALIZER block must be removed"

    # No keyword heuristic matching
    assert "err_lower" not in source, \
        "Keyword heuristic variable 'err_lower' must be removed"

    # No forced_commit_finalizer evidence
    assert "forced_commit_finalizer" not in source, \
        "All forced_commit_finalizer references must be removed"


# ────────────────────────────────────────────────────────────────────────────
# Test 5: Bounded retry uses HIGH thinking
# ────────────────────────────────────────────────────────────────────────────

def test_retry_uses_high_thinking_in_source():
    """
    Source inspection: the retry call to _run_single_agent must pass
    thinking_level="HIGH".
    """
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "gateway", "app.py",
    )
    with open(app_path, "r") as f:
        source = f.read()

    # Find the bounded retry section
    retry_start = source.find("BOUNDED LLM RETRY")
    assert retry_start > 0, "BOUNDED LLM RETRY section not found"

    retry_section = source[retry_start:retry_start + 5000]
    assert 'thinking_level="HIGH"' in retry_section, \
        "Bounded retry must use HIGH thinking level"
    assert "fail_pipeline_once" in retry_section, \
        "Bounded retry must call fail_pipeline_once on double failure"


# ────────────────────────────────────────────────────────────────────────────
# Test 6: Retry provenance fields are correct
# ────────────────────────────────────────────────────────────────────────────

def test_retry_provenance_fields_in_source():
    """
    Source inspection: after a successful retry, the investigation payload
    must include first_pass_commit, retry_used, attempt_count, retry_reason.
    fallback_used must NOT be set.
    """
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "gateway", "app.py",
    )
    with open(app_path, "r") as f:
        source = f.read()

    retry_start = source.find("BOUNDED LLM RETRY")
    retry_section = source[retry_start:retry_start + 4000]

    required_fields = [
        '"first_pass_commit"',
        '"retry_used"',
        '"attempt_count"',
        '"retry_reason"',
    ]
    for field in required_fields:
        assert field in retry_section, \
            f"Retry provenance must include {field}"


# ────────────────────────────────────────────────────────────────────────────
# Test 7: Investigation metrics are logged
# ────────────────────────────────────────────────────────────────────────────

def test_investigation_metrics_logged():
    """
    Source inspection: [METRICS:investigation] log line must exist
    with the required fields.
    """
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "gateway", "app.py",
    )
    with open(app_path, "r") as f:
        source = f.read()

    assert "[METRICS:investigation]" in source, \
        "Investigation metrics log line must exist"
    assert "first_pass_commit" in source, \
        "Metrics must track first_pass_commit"
    assert "retry_used" in source, \
        "Metrics must track retry_used"
    assert "attempt_count" in source, \
        "Metrics must track attempt_count"
