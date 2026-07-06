"""
tests/test_muhtasib_retry.py – Deterministic tests for Muhtasib bounded retry
================================================================================

Two scenarios:
  1. First attempt skips commit_verdict, retry commits → proceeds to AWAITING_APPROVAL.
  2. Both attempts skip commit_verdict → ESCALATED, no contract issued, no execution.

Agents are stubbed at the _run_single_agent boundary — NO Gemini calls.
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
    monkeypatch.setenv("MUHAFIZ_DB_PATH", str(tmp_path / "test_retry.db"))
    monkeypatch.setenv("VICTIM_SERVICE_URL", "http://localhost:9000")


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _nigehban_stub(state, _msg):
    return {
        **state,
        "triage_result": {
            "is_actionable": True,
            "severity_confirmed": "P1",
            "confidence": 0.95,
        },
    }


def _muhaqqiq_stub(state, _msg):
    return {
        **state,
        "investigation_result": {
            "root_cause_code": "BAD_DEPLOYMENT",
            "evidence": [{"source": "logs", "data": "regression", "trust": "direct"}],
            "tool_calls_made": ["get_cloud_logging_traces", "get_github_deployments"],
            "confidence": 0.9,
            "affected_components": ["auth-service"],
            "root_cause_summary": "Bad deployment caused JWT failures",
            "contributing_factors": [],
        },
        "investigation_event_hash": f"inv-hash-{uuid.uuid4().hex[:8]}",
    }


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
                    "target_revision": "rev-2024-06-19",
                    "order": 1,
                    "failure_policy": "STOP",
                }
            ],
        },
        "plan_event_hash": f"plan-hash-{uuid.uuid4().hex[:8]}",
    }


def _aamil_stub(state, _msg):
    return {
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


# ────────────────────────────────────────────────────────────────────────────
# Test 1: First attempt skips commit, retry commits → AWAITING_APPROVAL
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt():
    """
    Muhtasib first invocation fails to call commit_verdict (no new
    verdict_event_hash). The bounded retry invocation commits successfully.
    Expected: incident proceeds to AWAITING_APPROVAL, metadata shows
    first_pass_commit=False, retry_used=True.
    """
    from gateway.security import Settings, ApprovalTokenManager
    from gateway.store import IncidentStore
    from shared.dependencies import init_dependencies, get_store

    settings = Settings.from_env()
    settings.test_mode = True
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        import secrets as _s
        settings.approval_secret = _s.token_hex(32)

    token_manager = ApprovalTokenManager(settings.approval_secret)
    store = IncidentStore(settings.db_path)
    await store.initialize()
    init_dependencies(store=store, token_manager=token_manager, settings=settings)

    # Track muhtasib call count to distinguish first vs retry
    muhtasib_calls = {"count": 0}
    verdict_hash_counter = {"n": 0}

    async def mock_agent(agent_name: str, state: dict, message: str, thinking_level: str | None = None) -> dict:
        incident_id = state.get("incident_id", "")
        s = get_store()
        await _do_status_transitions(agent_name, state, s, incident_id)

        if agent_name == "nigehban":
            return _nigehban_stub(state, message)
        elif agent_name == "muhaqqiq":
            return _muhaqqiq_stub(state, message)
        elif agent_name == "mudabbir":
            return _mudabbir_stub(state, message)
        elif agent_name == "muhtasib":
            muhtasib_calls["count"] += 1
            if muhtasib_calls["count"] == 1:
                # First call: do NOT produce a new verdict_event_hash
                # (simulates the model failing to call commit_verdict)
                return state
            else:
                # Retry call: commit the verdict
                verdict_hash_counter["n"] += 1

                # Simulate what commit_verdict would do: persist event + transition
                verdict = {
                    "decision": "APPROVED_REQUIRES_HUMAN",
                    "risk_score": 0.2,
                    "reasoning": "Plan is safe: single rollback with known-good revision.",
                    "policy_findings": [],
                    "challenge": None,
                    "challenge_target": None,
                    "first_pass_commit": state.get("first_pass_commit", True),
                    "retry_used": state.get("retry_used", False),
                }

                event, _ = await s.append_event_and_room_message(
                    incident_id=incident_id,
                    run_id=state.get("run_id", ""),
                    actor="muhtasib",
                    actor_role="safety_reviewer",
                    event_type="verdict_issued",
                    summary="Safety verdict: APPROVED_REQUIRES_HUMAN (risk=0.20)",
                    payload=verdict,
                    room_sender="muhtasib",
                    room_content="⚖️ Safety review PASSED (retry).",
                    room_mentions=None,
                    room_message_type="verdict",
                    transition_from="REVIEWING",
                    transition_to="AWAITING_APPROVAL",
                )

                return {
                    **state,
                    "verdict": verdict,
                    "verdict_event_hash": event["event_hash"],
                }
        elif agent_name == "aamil":
            return _aamil_stub(state, message)
        return state

    with (
        patch("gateway.app._run_single_agent", side_effect=mock_agent),
        patch(
            "shared.recovery_verifier.verify_recovery",
            new_callable=AsyncMock,
            return_value=_mock_recovery,
        ),
    ):
        from httpx import AsyncClient, ASGITransport
        from gateway.app import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            alert_payload = {
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
            resp = await client.post("/api/incidents", json=alert_payload)
            assert resp.status_code == 201, f"Create failed: {resp.text}"
            incident_id = resp.json()["incident_id"]

            # Wait for Phase 1 to complete
            for _ in range(50):
                await asyncio.sleep(0.1)
                resp = await client.get(f"/api/incidents/{incident_id}")
                status = resp.json().get("status")
                if status in ("AWAITING_APPROVAL", "ESCALATED", "BLOCKED"):
                    break

            incident = resp.json()
            assert incident["status"] == "AWAITING_APPROVAL", (
                f"Expected AWAITING_APPROVAL, got {incident['status']}"
            )

            # Verify muhtasib was called exactly 2 times
            # (1 first-pass miss + 1 retry success)
            assert muhtasib_calls["count"] == 2, (
                f"Expected 2 muhtasib calls, got {muhtasib_calls['count']}"
            )

            # Verify the verdict has correct retry metadata
            events = await store.get_events(incident_id)
            verdict_events = [
                e for e in events
                if e.get("event_type") == "verdict_issued"
            ]
            assert len(verdict_events) >= 1, "No verdict event found"
            verdict_payload = json.loads(
                verdict_events[-1].get("payload_json", "{}")
            )
            assert verdict_payload["first_pass_commit"] is False, (
                "first_pass_commit should be False (retry committed)"
            )
            assert verdict_payload["retry_used"] is True, (
                "retry_used should be True"
            )

    print("✅ test_retry_succeeds_on_second_attempt passed")


# ────────────────────────────────────────────────────────────────────────────
# Test 2: Both attempts skip commit → ESCALATED, no contract, no execution
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_both_attempts_fail_escalates():
    """
    Both first and retry Muhtasib invocations fail to call commit_verdict.
    Expected: incident transitions to ESCALATED via forced finalizer,
    no contract is issued, no execution occurs. Metadata shows
    first_pass_commit=False, retry_used=True, decision=ESCALATE.
    """
    from gateway.security import Settings, ApprovalTokenManager
    from gateway.store import IncidentStore
    from shared.dependencies import init_dependencies, get_store

    settings = Settings.from_env()
    settings.test_mode = True
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        import secrets as _s
        settings.approval_secret = _s.token_hex(32)

    token_manager = ApprovalTokenManager(settings.approval_secret)
    store = IncidentStore(settings.db_path)
    await store.initialize()
    init_dependencies(store=store, token_manager=token_manager, settings=settings)

    muhtasib_calls = {"count": 0}

    async def mock_agent(agent_name: str, state: dict, message: str, thinking_level: str | None = None) -> dict:
        incident_id = state.get("incident_id", "")
        s = get_store()
        await _do_status_transitions(agent_name, state, s, incident_id)

        if agent_name == "nigehban":
            return _nigehban_stub(state, message)
        elif agent_name == "muhaqqiq":
            return _muhaqqiq_stub(state, message)
        elif agent_name == "mudabbir":
            return _mudabbir_stub(state, message)
        elif agent_name == "muhtasib":
            muhtasib_calls["count"] += 1
            # BOTH calls: do NOT produce a new verdict_event_hash
            return state
        elif agent_name == "aamil":
            # Should NOT be called
            raise AssertionError("Aamil should not be invoked when escalated")
        return state

    with (
        patch("gateway.app._run_single_agent", side_effect=mock_agent),
        patch(
            "shared.recovery_verifier.verify_recovery",
            new_callable=AsyncMock,
            return_value=_mock_recovery,
        ),
    ):
        from httpx import AsyncClient, ASGITransport
        from gateway.app import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            alert_payload = {
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
            resp = await client.post("/api/incidents", json=alert_payload)
            assert resp.status_code == 201, f"Create failed: {resp.text}"
            incident_id = resp.json()["incident_id"]

            # Wait for Phase 1 to complete
            for _ in range(50):
                await asyncio.sleep(0.1)
                resp = await client.get(f"/api/incidents/{incident_id}")
                status = resp.json().get("status")
                if status in ("AWAITING_APPROVAL", "ESCALATED", "BLOCKED"):
                    break

            incident = resp.json()
            assert incident["status"] == "ESCALATED", (
                f"Expected ESCALATED, got {incident['status']}"
            )

            # Verify muhtasib was called exactly 2 times
            # (1 first-pass miss + 1 retry miss)
            assert muhtasib_calls["count"] == 2, (
                f"Expected 2 muhtasib calls, got {muhtasib_calls['count']}"
            )

            # Verify NO contract was issued
            contract = await store.get_active_contract(incident_id)
            assert contract is None, (
                f"No contract should be issued when escalated, got {contract}"
            )

            # Verify no execution events
            events = await store.get_events(incident_id)
            execution_events = [
                e for e in events
                if e.get("event_type") == "actions_executed"
            ]
            assert len(execution_events) == 0, (
                "No execution events should exist when escalated"
            )

            # Verify the forced verdict has correct metadata
            verdict_events = [
                e for e in events
                if e.get("event_type") == "safety_review_completed"
            ]
            assert len(verdict_events) >= 1, "No safety_review_completed event"
            verdict_payload = json.loads(
                verdict_events[-1].get("payload_json", "{}")
            )
            assert verdict_payload["decision"] == "ESCALATE", (
                f"Expected ESCALATE, got {verdict_payload['decision']}"
            )
            assert verdict_payload["first_pass_commit"] is False, (
                "first_pass_commit should be False"
            )
            assert verdict_payload["retry_used"] is True, (
                "retry_used should be True"
            )

    print("✅ test_both_attempts_fail_escalates passed")


# ────────────────────────────────────────────────────────────────────────────
# Test 3: 3 CHALLENGE rounds → 3rd revision receives review → APPROVED
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_three_challenges_then_approved():
    """
    Muhtasib issues CHALLENGE 3 times.  After each challenge, Mudabbir
    produces a revised plan.  The 4th Muhtasib review sees the 3rd
    revision and approves it.
    Expected: incident reaches AWAITING_APPROVAL, challenges_used=3.
    """
    from gateway.security import Settings, ApprovalTokenManager
    from gateway.store import IncidentStore
    from shared.dependencies import init_dependencies, get_store

    settings = Settings.from_env()
    settings.test_mode = True
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        import secrets as _s
        settings.approval_secret = _s.token_hex(32)

    token_manager = ApprovalTokenManager(settings.approval_secret)
    store = IncidentStore(settings.db_path)
    await store.initialize()
    init_dependencies(store=store, token_manager=token_manager, settings=settings)

    muhtasib_reviews = {"count": 0}
    mudabbir_revisions = {"count": 0}

    async def mock_agent(
        agent_name: str, state: dict, message: str,
        thinking_level: str | None = None,
    ) -> dict:
        incident_id = state.get("incident_id", "")
        s = get_store()
        await _do_status_transitions(agent_name, state, s, incident_id)

        if agent_name == "nigehban":
            return _nigehban_stub(state, message)
        elif agent_name == "muhaqqiq":
            return _muhaqqiq_stub(state, message)
        elif agent_name == "mudabbir":
            mudabbir_revisions["count"] += 1
            result = _mudabbir_stub(state, message)
            result["plan"]["revision"] = mudabbir_revisions["count"]
            result["plan_event_hash"] = f"plan-rev-{mudabbir_revisions['count']}-{uuid.uuid4().hex[:8]}"
            return result
        elif agent_name == "muhtasib":
            muhtasib_reviews["count"] += 1
            # First 3 reviews: CHALLENGE. 4th review: APPROVE.
            if muhtasib_reviews["count"] <= 3:
                verdict = {
                    "decision": "CHALLENGE",
                    "risk_score": 0.6,
                    "reasoning": f"Challenge round {muhtasib_reviews['count']}: plan needs improvement",
                    "policy_findings": ["needs-revision"],
                    "challenge": f"Plan issue round {muhtasib_reviews['count']}",
                    "challenge_target": "PLAN",
                    "first_pass_commit": True,
                    "retry_used": False,
                }
            else:
                verdict = {
                    "decision": "APPROVED_REQUIRES_HUMAN",
                    "risk_score": 0.15,
                    "reasoning": "Plan is safe after 3 rounds of revision.",
                    "policy_findings": [],
                    "challenge": None,
                    "challenge_target": None,
                    "first_pass_commit": True,
                    "retry_used": False,
                }

            transition_to = (
                "PLANNING" if verdict["decision"] == "CHALLENGE"
                else "AWAITING_APPROVAL"
            )
            event, _ = await s.append_event_and_room_message(
                incident_id=incident_id,
                run_id=state.get("run_id", ""),
                actor="muhtasib",
                actor_role="safety_reviewer",
                event_type="verdict_issued",
                summary=f"Safety verdict: {verdict['decision']}",
                payload=verdict,
                room_sender="muhtasib",
                room_content=f"⚖️ Verdict: {verdict['decision']}",
                room_mentions=None,
                room_message_type="verdict",
                transition_from="REVIEWING",
                transition_to=transition_to,
            )
            return {
                **state,
                "verdict": verdict,
                "verdict_event_hash": event["event_hash"],
            }
        elif agent_name == "aamil":
            return _aamil_stub(state, message)
        return state

    with (
        patch("gateway.app._run_single_agent", side_effect=mock_agent),
        patch(
            "shared.recovery_verifier.verify_recovery",
            new_callable=AsyncMock,
            return_value=_mock_recovery,
        ),
    ):
        from httpx import AsyncClient, ASGITransport
        from gateway.app import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            alert_payload = {
                "alert": {
                    "alert_type": "error_rate",
                    "service_id": "auth-service",
                    "severity": "P1",
                    "summary": "Error rate spike",
                    "error_message": "JWT failures",
                    "timestamp": "2026-06-21T12:00:00Z",
                },
                "scenario_id": "bad_deployment",
            }
            resp = await client.post("/api/incidents", json=alert_payload)
            assert resp.status_code == 201
            incident_id = resp.json()["incident_id"]

            for _ in range(80):
                await asyncio.sleep(0.1)
                resp = await client.get(f"/api/incidents/{incident_id}")
                status = resp.json().get("status")
                if status in ("AWAITING_APPROVAL", "ESCALATED", "BLOCKED"):
                    break

            incident = resp.json()
            assert incident["status"] == "AWAITING_APPROVAL", (
                f"Expected AWAITING_APPROVAL after 3 challenges + approval, got {incident['status']}"
            )
            # 4 muhtasib reviews: 3 challenges + 1 approval
            assert muhtasib_reviews["count"] == 4, (
                f"Expected 4 muhtasib reviews, got {muhtasib_reviews['count']}"
            )
            # 4 mudabbir calls: 1 initial + 3 revisions
            assert mudabbir_revisions["count"] == 4, (
                f"Expected 4 mudabbir calls, got {mudabbir_revisions['count']}"
            )

    print("✅ test_three_challenges_then_approved passed")


# ────────────────────────────────────────────────────────────────────────────
# Test 4: 4th CHALLENGE → escalation WITHOUT another revision
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fourth_challenge_escalates_without_revision():
    """
    Muhtasib issues CHALLENGE on every review.  After 3 challenges have
    consumed the budget, the 4th challenge must trigger immediate
    escalation WITHOUT producing another Mudabbir revision.
    Expected: ESCALATED, challenge_limit_reached event, no 4th revision.
    """
    from gateway.security import Settings, ApprovalTokenManager
    from gateway.store import IncidentStore
    from shared.dependencies import init_dependencies, get_store

    settings = Settings.from_env()
    settings.test_mode = True
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        import secrets as _s
        settings.approval_secret = _s.token_hex(32)

    token_manager = ApprovalTokenManager(settings.approval_secret)
    store = IncidentStore(settings.db_path)
    await store.initialize()
    init_dependencies(store=store, token_manager=token_manager, settings=settings)

    muhtasib_reviews = {"count": 0}
    mudabbir_revisions = {"count": 0}

    async def mock_agent(
        agent_name: str, state: dict, message: str,
        thinking_level: str | None = None,
    ) -> dict:
        incident_id = state.get("incident_id", "")
        s = get_store()
        await _do_status_transitions(agent_name, state, s, incident_id)

        if agent_name == "nigehban":
            return _nigehban_stub(state, message)
        elif agent_name == "muhaqqiq":
            return _muhaqqiq_stub(state, message)
        elif agent_name == "mudabbir":
            mudabbir_revisions["count"] += 1
            result = _mudabbir_stub(state, message)
            result["plan"]["revision"] = mudabbir_revisions["count"]
            result["plan_event_hash"] = f"plan-rev-{mudabbir_revisions['count']}-{uuid.uuid4().hex[:8]}"
            return result
        elif agent_name == "muhtasib":
            muhtasib_reviews["count"] += 1
            # ALWAYS challenge — never approve
            verdict = {
                "decision": "CHALLENGE",
                "risk_score": 0.7,
                "reasoning": f"Perpetual challenge round {muhtasib_reviews['count']}",
                "policy_findings": ["still-unsafe"],
                "challenge": f"Plan still bad round {muhtasib_reviews['count']}",
                "challenge_target": "PLAN",
                "first_pass_commit": True,
                "retry_used": False,
            }
            event, _ = await s.append_event_and_room_message(
                incident_id=incident_id,
                run_id=state.get("run_id", ""),
                actor="muhtasib",
                actor_role="safety_reviewer",
                event_type="verdict_issued",
                summary=f"Safety verdict: CHALLENGE round {muhtasib_reviews['count']}",
                payload=verdict,
                room_sender="muhtasib",
                room_content=f"⚖️ CHALLENGE round {muhtasib_reviews['count']}",
                room_mentions=["mudabbir"],
                room_message_type="verdict",
                transition_from="REVIEWING",
                transition_to="PLANNING",
            )
            return {
                **state,
                "verdict": verdict,
                "verdict_event_hash": event["event_hash"],
            }
        elif agent_name == "aamil":
            raise AssertionError("Aamil should not be invoked when challenge-escalated")
        return state

    with (
        patch("gateway.app._run_single_agent", side_effect=mock_agent),
        patch(
            "shared.recovery_verifier.verify_recovery",
            new_callable=AsyncMock,
            return_value=_mock_recovery,
        ),
    ):
        from httpx import AsyncClient, ASGITransport
        from gateway.app import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            alert_payload = {
                "alert": {
                    "alert_type": "error_rate",
                    "service_id": "auth-service",
                    "severity": "P1",
                    "summary": "Error rate spike",
                    "error_message": "JWT failures",
                    "timestamp": "2026-06-21T12:00:00Z",
                },
                "scenario_id": "bad_deployment",
            }
            resp = await client.post("/api/incidents", json=alert_payload)
            assert resp.status_code == 201
            incident_id = resp.json()["incident_id"]

            for _ in range(80):
                await asyncio.sleep(0.1)
                resp = await client.get(f"/api/incidents/{incident_id}")
                status = resp.json().get("status")
                if status in ("AWAITING_APPROVAL", "ESCALATED", "BLOCKED"):
                    break

            incident = resp.json()
            assert incident["status"] == "ESCALATED", (
                f"Expected ESCALATED after 4th challenge, got {incident['status']}"
            )

            # 4 muhtasib reviews: 3 challenges that triggered revisions
            # + 1 challenge that hit the limit
            assert muhtasib_reviews["count"] == 4, (
                f"Expected 4 muhtasib reviews, got {muhtasib_reviews['count']}"
            )

            # CRITICAL: only 4 mudabbir calls (1 initial + 3 revisions)
            # The 4th challenge must NOT produce a 4th revision.
            assert mudabbir_revisions["count"] == 4, (
                f"Expected 4 mudabbir calls (1 initial + 3 revisions), "
                f"got {mudabbir_revisions['count']}. "
                f"4th challenge should not generate another revision."
            )

            # Verify challenge_limit_reached event exists
            events = await store.get_events(incident_id)
            limit_events = [
                e for e in events
                if e.get("event_type") == "challenge_limit_reached"
            ]
            assert len(limit_events) >= 1, (
                "Expected challenge_limit_reached event"
            )

            # Verify no contract was issued
            contract = await store.get_active_contract(incident_id)
            assert contract is None, (
                "No contract should be issued when challenge-escalated"
            )

    print("✅ test_fourth_challenge_escalates_without_revision passed")

