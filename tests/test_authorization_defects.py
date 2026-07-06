"""
tests/test_authorization_defects.py – Regression tests for authorization fixes.

Covers:
    - Concurrent approval race: exactly one winner, loser gets clear reason.
    - Plan tampering via canonical_plan_json: blocked, plan_tampered event emitted.
    - Plan tampering via actions_json: blocked independently.
    - Happy path: valid plan passes revalidation and transitions to EXECUTING.
    - Double-execution guard: second claim_execution_snapshot fails on EXECUTING contract.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import aiosqlite
import pytest
import pytest_asyncio

from gateway.models import canonical_json, sha256_hex
from gateway.security import (
    ApprovalTokenManager,
    build_token_claims,
    generate_approval_nonce,
)
from gateway.store import IncidentStore

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SECRET = "test-secret-that-is-at-least-32-chars-long"
TOKEN_MGR = ApprovalTokenManager(SECRET)


@pytest_asyncio.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = IncidentStore(db_path)
    await s.initialize()
    return s


async def _create_incident_with_issued_contract(
    store: IncidentStore,
) -> tuple[str, str, str, int, str, str]:
    """
    Create incident → force AWAITING_APPROVAL → issue contract.

    Returns (incident_id, run_id, contract_id, revision, plan_hash, token)
    """
    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"

    await store.create_incident(
        incident_id=incident_id,
        alert={
            "severity": "P1",
            "service_id": "auth-service",
            "summary": "Test incident",
            "alert_type": "deployment",
            "error_message": "500 errors",
            "timestamp": "2026-06-23T09:00:00+00:00",
        },
    )
    await store.transition_incident(incident_id, "DETECTED", "AWAITING_APPROVAL")

    plan = {
        "plan_id": f"PLAN-{uuid.uuid4().hex[:8].upper()}",
        "revision": 1,
        "strategy_summary": "Rollback bad deployment",
        "risk_level": "medium",
        "estimated_mttr_minutes": 5,
        "actions": [
            {
                "action_id": "act-001",
                "skill": "rollback_service_revision",
                "target": "auth-service",
                "arguments": {
                    "service_name": "auth-service",
                    "target_revision": "v1.2.3",
                },
                "depends_on": [],
                "on_failure": "STOP",
            }
        ],
    }
    plan_json = canonical_json(plan)
    plan_hash = sha256_hex(plan)
    actions_json = canonical_json(plan["actions"])
    nonce = generate_approval_nonce()

    claims = build_token_claims(
        incident_id=incident_id,
        contract_id="pending",
        revision=1,
        plan_hash=plan_hash,
        nonce=nonce,
        ttl_seconds=600,
    )
    token = TOKEN_MGR.generate_token(claims)
    digest = ApprovalTokenManager.token_digest(token)

    contract = await store.issue_contract(
        incident_id=incident_id,
        revision=1,
        plan_id=plan["plan_id"],
        plan_hash=plan_hash,
        plan_event_hash="abc123",
        canonical_plan_json=plan_json,
        actions_json=actions_json,
        approval_nonce=nonce,
        token_digest=digest,
        expires_at="2099-01-01T00:00:00+00:00",
        claims_json="",
    )
    contract_id = contract["contract_id"]

    claims["contract_id"] = contract_id
    claims_json = json.dumps(claims, sort_keys=True)
    final_token = TOKEN_MGR.generate_token(claims)
    final_digest = ApprovalTokenManager.token_digest(final_token)
    await store.transition_contract(
        incident_id=incident_id,
        revision=1,
        from_status="ISSUED",
        to_status="ISSUED",
        token_digest=final_digest,
        claims_json=claims_json,
    )
    await store.update_incident(incident_id, active_run_id=run_id)

    return incident_id, run_id, contract_id, 1, plan_hash, final_token


def _get_hmac_claims(contract: dict, plan_hash: str) -> dict:
    claims_json = contract.get("claims_json") or "{}"
    claims = json.loads(claims_json)
    if not claims.get("plan_hash"):
        claims["plan_hash"] = plan_hash
    return claims


# ---------------------------------------------------------------------------
# Defect 1: Concurrent approval – single winner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_approval_concurrent_single_winner(store):
    """
    Two concurrent claim_approval calls yield exactly one winner and one loser.
    Contract ends up in APPROVED, incident in APPROVED, one human_approved event.
    """
    incident_id, run_id, contract_id, revision, plan_hash, token = (
        await _create_incident_with_issued_contract(store)
    )

    payload = {
        "action": "APPROVE",
        "contract_id": contract_id,
        "revision": revision,
        "operator": "Operator",
        "feedback": None,
    }

    results = await asyncio.gather(
        store.claim_approval(
            incident_id=incident_id, contract_id=contract_id, revision=revision,
            approved_by="Operator", run_id=run_id, decision_payload=payload,
        ),
        store.claim_approval(
            incident_id=incident_id, contract_id=contract_id, revision=revision,
            approved_by="Operator", run_id=run_id, decision_payload=payload,
        ),
        return_exceptions=True,
    )

    winners = [r for r in results if isinstance(r, tuple) and r[0] is True]
    losers  = [r for r in results if isinstance(r, tuple) and r[0] is False]

    assert len(winners) == 1, f"Expected 1 winner, got {len(winners)}"
    assert len(losers)  == 1, f"Expected 1 loser, got {len(losers)}"
    assert losers[0][1] != "", "Loser must carry a non-empty reason"

    contract_row = await store.get_contract_by_id(contract_id)
    assert contract_row["status"] == "APPROVED"

    incident_row = await store.get_incident(incident_id)
    assert incident_row["status"] == "APPROVED"

    events = await store.get_events_by_type(incident_id, "human_approved")
    assert len(events) == 1, f"Exactly one human_approved event expected, got {len(events)}"


@pytest.mark.asyncio
async def test_claim_approval_sequential_second_returns_false(store):
    """Sequential second claim_approval must return (False, reason, {})."""
    incident_id, run_id, contract_id, revision, plan_hash, token = (
        await _create_incident_with_issued_contract(store)
    )
    payload = {"action": "APPROVE", "contract_id": contract_id, "revision": revision,
               "operator": "Op", "feedback": None}

    ok1, r1, ev1 = await store.claim_approval(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        approved_by="Op", run_id=run_id, decision_payload=payload,
    )
    assert ok1 is True, f"First call should win; got reason={r1}"

    ok2, r2, ev2 = await store.claim_approval(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        approved_by="Op", run_id=run_id, decision_payload=payload,
    )
    assert ok2 is False
    assert r2 != "", "Second call must have a reason string"


# ---------------------------------------------------------------------------
# Defect 2a: canonical_plan_json tamper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_plan_json_tamper_blocks_execution(store):
    """
    Tamper canonical_plan_json → hash no longer matches plan_hash →
    claim_execution_snapshot returns (False, 'canonical_plan_hash_mismatch', {}).
    After invalidate_tampered_contract: contract=INVALIDATED, incident=BLOCKED,
    plan_tampered event emitted, zero plan_validated events.
    """
    incident_id, run_id, contract_id, revision, plan_hash, token = (
        await _create_incident_with_issued_contract(store)
    )

    # Legitimate approval
    payload = {"action": "APPROVE", "contract_id": contract_id, "revision": revision,
               "operator": "Op", "feedback": None}
    ok, _, _ = await store.claim_approval(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        approved_by="Op", run_id=run_id, decision_payload=payload,
    )
    assert ok is True

    # Tamper canonical_plan_json
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        tampered = json.dumps({"actions": [{"skill": "EVIL_SKILL", "target": "all"}]})
        await db.execute(
            "UPDATE approval_contracts SET canonical_plan_json = ? WHERE contract_id = ?",
            (tampered, contract_id),
        )
        await db.commit()

    contract_row = await store.get_contract_by_id(contract_id)
    hmac_claims = _get_hmac_claims(contract_row, plan_hash)

    ok2, reason2, snap2 = await store.claim_execution_snapshot(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        run_id=run_id, hmac_claims=hmac_claims, token_manager=TOKEN_MGR,
    )

    assert ok2 is False, "Tampered canonical_plan_json must be rejected"
    assert "canonical_plan_hash_mismatch" in reason2, f"Unexpected reason: {reason2}"

    # Caller's responsibility: atomically invalidate
    await store.invalidate_tampered_contract(
        incident_id=incident_id, contract_id=contract_id,
        run_id=run_id, reason=reason2,
    )

    contract_after = await store.get_contract_by_id(contract_id)
    assert contract_after["status"] == "INVALIDATED"

    incident_after = await store.get_incident(incident_id)
    assert incident_after["status"] == "BLOCKED"

    tamper_events = await store.get_events_by_type(incident_id, "plan_tampered")
    assert len(tamper_events) == 1

    validated_events = await store.get_events_by_type(incident_id, "plan_validated")
    assert len(validated_events) == 0, "No plan_validated event when tampered"


# ---------------------------------------------------------------------------
# Defect 2b: actions_json tamper (independent of canonical_plan_json)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actions_json_tamper_blocks_execution(store):
    """
    Tamper actions_json while leaving canonical_plan_json intact →
    claim_execution_snapshot returns (False, 'actions_json_divergence', {}).
    """
    incident_id, run_id, contract_id, revision, plan_hash, token = (
        await _create_incident_with_issued_contract(store)
    )

    payload = {"action": "APPROVE", "contract_id": contract_id, "revision": revision,
               "operator": "Op", "feedback": None}
    ok, _, _ = await store.claim_approval(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        approved_by="Op", run_id=run_id, decision_payload=payload,
    )
    assert ok is True

    # Tamper only actions_json
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        tampered_actions = json.dumps([{"skill": "EVIL_SKILL"}])
        await db.execute(
            "UPDATE approval_contracts SET actions_json = ? WHERE contract_id = ?",
            (tampered_actions, contract_id),
        )
        await db.commit()

    contract_row = await store.get_contract_by_id(contract_id)
    hmac_claims = _get_hmac_claims(contract_row, plan_hash)

    ok2, reason2, snap2 = await store.claim_execution_snapshot(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        run_id=run_id, hmac_claims=hmac_claims, token_manager=TOKEN_MGR,
    )

    assert ok2 is False, "Tampered actions_json must be rejected"
    assert "actions_json_divergence" in reason2, f"Unexpected reason: {reason2}"


# ---------------------------------------------------------------------------
# Happy path: clean plan passes revalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_plan_passes_revalidation(store):
    """
    An unmodified contract passes claim_execution_snapshot; snapshot has
    correct plan_hash, actions from canonical source, and plan_validated event.
    """
    incident_id, run_id, contract_id, revision, plan_hash, token = (
        await _create_incident_with_issued_contract(store)
    )

    payload = {"action": "APPROVE", "contract_id": contract_id, "revision": revision,
               "operator": "Op", "feedback": None}
    await store.claim_approval(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        approved_by="Op", run_id=run_id, decision_payload=payload,
    )

    contract_row = await store.get_contract_by_id(contract_id)
    hmac_claims = _get_hmac_claims(contract_row, plan_hash)

    ok, reason, snapshot = await store.claim_execution_snapshot(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        run_id=run_id, hmac_claims=hmac_claims, token_manager=TOKEN_MGR,
    )

    assert ok is True, f"Valid plan should pass, got reason={reason}"
    assert snapshot["plan_hash"] == plan_hash
    # Actions come exclusively from canonical_plan
    assert len(snapshot["actions"]) == 1
    assert snapshot["actions"][0]["skill"] == "rollback_service_revision"

    contract_after = await store.get_contract_by_id(contract_id)
    assert contract_after["status"] == "EXECUTING"

    incident_after = await store.get_incident(incident_id)
    assert incident_after["status"] == "EXECUTING"

    events = await store.get_events_by_type(incident_id, "plan_validated")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_second_claim_execution_snapshot_blocked_if_already_executing(store):
    """
    Once a contract is EXECUTING, a second claim_execution_snapshot returns False,
    guaranteeing at-most-one Phase 2 execution.
    """
    incident_id, run_id, contract_id, revision, plan_hash, token = (
        await _create_incident_with_issued_contract(store)
    )

    payload = {"action": "APPROVE", "contract_id": contract_id, "revision": revision,
               "operator": "Op", "feedback": None}
    await store.claim_approval(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        approved_by="Op", run_id=run_id, decision_payload=payload,
    )

    contract_row = await store.get_contract_by_id(contract_id)
    hmac_claims = _get_hmac_claims(contract_row, plan_hash)
    kwargs = dict(
        incident_id=incident_id, contract_id=contract_id, revision=revision,
        run_id=run_id, hmac_claims=hmac_claims, token_manager=TOKEN_MGR,
    )

    ok1, _, _ = await store.claim_execution_snapshot(**kwargs)
    assert ok1 is True

    ok2, reason2, _ = await store.claim_execution_snapshot(**kwargs)
    assert ok2 is False
    assert reason2 != ""  # contract_not_approved:EXECUTING or similar
