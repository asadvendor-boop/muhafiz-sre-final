"""
tests/test_architectural_invariants.py – Architectural strictness tests

Verifies:
    - Every AGENT_REGISTRY member is an LlmAgent (not composite orchestrator)
    - No composite orchestrator imports in agent code files
    - Aamil executes solely from execution_snapshot even when get_active_contract raises
    - Chain-replay audit verifier validates event integrity
"""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
from shared.chain_verifier import verify_chain

SECRET = "test-secret-that-is-at-least-32-chars-long"
TOKEN_MGR = ApprovalTokenManager(SECRET)

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = IncidentStore(db_path)
    await s.initialize()
    return s


async def _setup_executing_incident(store):
    """Create incident → AWAITING_APPROVAL → issue contract → approve → return snapshot."""
    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"

    await store.create_incident(
        incident_id=incident_id,
        alert={
            "severity": "P1", "service_id": "auth-service",
            "summary": "Test", "alert_type": "deployment",
            "error_message": "500", "timestamp": "2026-06-23T09:00:00+00:00",
        },
    )
    await store.transition_incident(incident_id, "DETECTED", "AWAITING_APPROVAL")

    plan = {
        "plan_id": f"PLAN-{uuid.uuid4().hex[:8].upper()}",
        "revision": 1,
        "strategy_summary": "Rollback",
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
        incident_id=incident_id, contract_id="pending",
        revision=1, plan_hash=plan_hash, nonce=nonce, ttl_seconds=600,
    )
    token = TOKEN_MGR.generate_token(claims)
    digest = ApprovalTokenManager.token_digest(token)

    contract = await store.issue_contract(
        incident_id=incident_id, revision=1,
        plan_id=plan["plan_id"], plan_hash=plan_hash,
        plan_event_hash="abc123", canonical_plan_json=plan_json,
        actions_json=actions_json, approval_nonce=nonce,
        token_digest=digest, expires_at="2099-01-01T00:00:00+00:00",
        claims_json="",
    )
    contract_id = contract["contract_id"]

    claims["contract_id"] = contract_id
    claims_json_str = json.dumps(claims, sort_keys=True)
    final_token = TOKEN_MGR.generate_token(claims)
    final_digest = ApprovalTokenManager.token_digest(final_token)
    await store.transition_contract(
        incident_id=incident_id, revision=1,
        from_status="ISSUED", to_status="ISSUED",
        token_digest=final_digest, claims_json=claims_json_str,
    )
    await store.update_incident(incident_id, active_run_id=run_id)

    # Approve
    ok, _, _ = await store.claim_approval(
        incident_id=incident_id, contract_id=contract_id, revision=1,
        approved_by="Op", run_id=run_id,
        decision_payload={"action": "APPROVE"},
    )
    assert ok

    # Claim execution snapshot
    hmac_claims = json.loads(claims_json_str)
    ok2, _, snapshot = await store.claim_execution_snapshot(
        incident_id=incident_id, contract_id=contract_id, revision=1,
        run_id=run_id, hmac_claims=hmac_claims, token_manager=TOKEN_MGR,
    )
    assert ok2

    return incident_id, run_id, snapshot, store


# ===========================================================================
# Test 1: Every AGENT_REGISTRY member is an LlmAgent
# ===========================================================================

def test_agent_registry_llmagent_only():
    """Every agent in AGENT_REGISTRY must be a plain LlmAgent."""
    from google.adk.agents import LlmAgent
    from agents.agent import AGENT_REGISTRY

    for name, agent in AGENT_REGISTRY.items():
        assert isinstance(agent, LlmAgent), (
            f"AGENT_REGISTRY['{name}'] is {type(agent).__name__}, expected LlmAgent"
        )


# ===========================================================================
# Test 2: No composite orchestrator imports in agent source files
# ===========================================================================

def test_no_composite_orchestrator_imports():
    """
    Agent source files must not import LoopAgent, SequentialAgent, or GroupAgent.
    Scan the AST of each .py file in agents/ for these imports.
    """
    forbidden = {"LoopAgent", "SequentialAgent", "GroupAgent"}
    violations = []

    for py_file in AGENTS_DIR.glob("*.py"):
        source = py_file.read_text()
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    real_name = alias.name
                    if real_name in forbidden:
                        violations.append(
                            f"{py_file.name}: imports {real_name}"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if any(p in forbidden for p in parts):
                        violations.append(
                            f"{py_file.name}: imports {alias.name}"
                        )

    assert not violations, (
        "Forbidden composite orchestrator imports found:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ===========================================================================
# Test 3: Aamil executes from snapshot even when get_active_contract raises
# ===========================================================================

@pytest.mark.asyncio
async def test_aamil_executes_from_snapshot_not_store(store):
    """
    Prove Aamil reads actions from execution_snapshot, not the database.

    We patch store.get_active_contract to RAISE, then invoke
    execute_approved_actions with a valid snapshot in session state.
    Aamil must succeed — proving it never touches get_active_contract.
    """
    incident_id, run_id, snapshot, store = await _setup_executing_incident(store)

    # Import and invoke the tool function directly
    from agents.aamil import execute_approved_actions

    # Build a mock ToolContext
    mock_ctx = AsyncMock()
    mock_ctx.state = {
        "incident_id": incident_id,
        "run_id": run_id,
        "execution_snapshot": snapshot,
    }

    # Patch get_active_contract to explode — proves Aamil never calls it
    with patch.object(
        store, "get_active_contract",
        side_effect=RuntimeError("MUST NOT BE CALLED — gateway is sole authority"),
    ):
        # Patch get_store at the module Aamil imports from
        with patch(
            "shared.dependencies.get_store", return_value=store,
        ):
            result = await execute_approved_actions(mock_ctx)

    # Aamil should have executed successfully from the snapshot
    assert result["status"] == "execution_complete", (
        f"Expected execution_complete, got: {result}"
    )
    assert result["actions_executed"] == 1
    assert result["all_succeeded"] is True

    # Verify session state was populated
    assert "execution_receipts" in mock_ctx.state
    assert mock_ctx.state["all_actions_succeeded"] is True


@pytest.mark.asyncio
async def test_aamil_fails_without_snapshot():
    """
    Without execution_snapshot in session state, Aamil must return
    an authority boundary violation error — not silently fetch from DB.
    """
    from agents.aamil import execute_approved_actions

    mock_ctx = AsyncMock()
    mock_ctx.state = {
        "incident_id": "INC-MISSING",
        "run_id": "RUN-MISSING",
        # No execution_snapshot
    }

    result = await execute_approved_actions(mock_ctx)
    assert result["status"] == "error"
    assert "authority boundary violation" in result["message"].lower()


# ===========================================================================
# Test 4: Chain-replay audit verifier
# ===========================================================================

@pytest.mark.asyncio
async def test_chain_verifier_valid_chain(store):
    """
    After a full approval + execution snapshot flow, the chain verifier
    must report valid=True and correct facts.
    """
    incident_id, run_id, snapshot, store = await _setup_executing_incident(store)

    result = await verify_chain(store, incident_id)

    assert result.valid is True, "Chain integrity must be valid"
    assert result.event_count >= 2, f"Expected ≥2 events, got {result.event_count}"
    assert "human_approved" in result.event_types
    assert "plan_validated" in result.event_types
    assert result.facts["approval_claimed"] is True
    assert result.facts["plan_validated"] is True
    assert result.facts["plan_tampered"] is False
    assert result.chain_head_hash != ""
    assert result.chain_tail_hash != ""


@pytest.mark.asyncio
async def test_chain_verifier_after_tamper_invalidation(store):
    """
    After a tamper detection + invalidation, the chain must still be
    valid (hashes intact) and report plan_tampered=True.
    """
    # Create a fresh incident for tamper test
    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"

    await store.create_incident(
        incident_id=incident_id,
        alert={
            "severity": "P1", "service_id": "auth-svc",
            "summary": "T", "alert_type": "deployment",
            "error_message": "500", "timestamp": "2026-06-23T09:00:00+00:00",
        },
    )
    await store.transition_incident(incident_id, "DETECTED", "AWAITING_APPROVAL")

    plan = {
        "plan_id": "PLAN-TAMPER",
        "revision": 1,
        "strategy_summary": "Rollback",
        "risk_level": "medium",
        "estimated_mttr_minutes": 5,
        "actions": [{"action_id": "a1", "skill": "rollback_service_revision",
                      "target": "auth-svc",
                      "arguments": {"service_name": "auth-svc", "target_revision": "v1"},
                      "depends_on": [], "on_failure": "STOP"}],
    }
    plan_json = canonical_json(plan)
    plan_hash = sha256_hex(plan)
    actions_json = canonical_json(plan["actions"])
    nonce = generate_approval_nonce()

    claims = build_token_claims(
        incident_id=incident_id, contract_id="pending",
        revision=1, plan_hash=plan_hash, nonce=nonce, ttl_seconds=600,
    )
    token = TOKEN_MGR.generate_token(claims)
    digest = ApprovalTokenManager.token_digest(token)
    contract = await store.issue_contract(
        incident_id=incident_id, revision=1,
        plan_id="PLAN-TAMPER", plan_hash=plan_hash,
        plan_event_hash="abc", canonical_plan_json=plan_json,
        actions_json=actions_json, approval_nonce=nonce,
        token_digest=digest, expires_at="2099-01-01T00:00:00+00:00",
        claims_json="",
    )
    contract_id = contract["contract_id"]

    claims["contract_id"] = contract_id
    claims_json_str = json.dumps(claims, sort_keys=True)
    final_token = TOKEN_MGR.generate_token(claims)
    final_digest = ApprovalTokenManager.token_digest(final_token)
    await store.transition_contract(
        incident_id=incident_id, revision=1,
        from_status="ISSUED", to_status="ISSUED",
        token_digest=final_digest, claims_json=claims_json_str,
    )
    await store.update_incident(incident_id, active_run_id=run_id)

    await store.claim_approval(
        incident_id=incident_id, contract_id=contract_id, revision=1,
        approved_by="Op", run_id=run_id, decision_payload={"action": "APPROVE"},
    )

    # Tamper canonical_plan_json
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "UPDATE approval_contracts SET canonical_plan_json = ? WHERE contract_id = ?",
            (json.dumps({"actions": [{"skill": "EVIL"}]}), contract_id),
        )
        await db.commit()

    hmac_claims = json.loads(claims_json_str)
    ok, reason, _ = await store.claim_execution_snapshot(
        incident_id=incident_id, contract_id=contract_id, revision=1,
        run_id=run_id, hmac_claims=hmac_claims, token_manager=TOKEN_MGR,
    )
    assert not ok

    await store.invalidate_tampered_contract(
        incident_id=incident_id, contract_id=contract_id,
        run_id=run_id, reason=reason,
    )

    # Verify chain
    result = await verify_chain(store, incident_id)
    assert result.valid is True, "Hash chain itself must still be valid"
    assert result.facts["plan_tampered"] is True
    assert "plan_tampered" in result.event_types
    assert result.terminal_status == "BLOCKED"


# ── Test: Agent Model + Thinking Level Matrix ────────────────────────────────

def test_agent_model_and_thinking_matrix():
    """Verify every agent factory produces the validated model + thinking config.

    Expected runtime matrix (21/21 benchmark-validated):
        Nigehban   gemini-3.1-flash-lite    LOW
        Muhaqqiq   gemini-3-flash-preview   MEDIUM
        Mudabbir   gemini-3-flash-preview   MEDIUM
        Muhtasib   gemini-3.1-pro-preview   HIGH
        Aamil      gemini-3.1-flash-lite    MINIMAL
    """
    from agents.nigehban import get_nigehban_agent
    from agents.muhaqqiq import get_muhaqqiq_agent
    from agents.mudabbir import get_mudabbir_agent
    from agents.muhtasib import get_muhtasib_agent
    from agents.aamil import get_aamil_agent

    EXPECTED = {
        "nigehban": {
            "factory": lambda: get_nigehban_agent(),
            "model": "gemini-3.1-flash-lite",
            "thinking": "LOW",
        },
        "muhaqqiq": {
            "factory": lambda: get_muhaqqiq_agent("bad_deployment"),
            "model": "gemini-3-flash-preview",
            "thinking": "MEDIUM",
        },
        "mudabbir": {
            "factory": lambda: get_mudabbir_agent(),
            "model": "gemini-3-flash-preview",
            "thinking": "MEDIUM",
        },
        "muhtasib": {
            "factory": lambda: get_muhtasib_agent(),
            "model": "gemini-3.1-pro-preview",
            "thinking": "HIGH",
        },
        "aamil": {
            "factory": lambda: get_aamil_agent(),
            "model": "gemini-3.1-flash-lite",
            "thinking": "MINIMAL",
        },
    }

    for name, spec in EXPECTED.items():
        agent = spec["factory"]()
        assert agent.model == spec["model"], (
            f"{name}: expected model={spec['model']!r}, got {agent.model!r}"
        )
        config = agent.generate_content_config
        assert config is not None, f"{name}: missing generate_content_config"
        tc = config.thinking_config
        assert tc is not None, f"{name}: missing thinking_config"
        level = str(tc.thinking_level)
        # Accept both "HIGH" and "ThinkingLevel.HIGH" formats
        assert spec["thinking"] in level, (
            f"{name}: expected thinking={spec['thinking']!r} in {level!r}"
        )

    # Verify adaptive overrides produce correct levels
    muhaqqiq_high = get_muhaqqiq_agent("bad_deployment", thinking_level="HIGH")
    assert "HIGH" in str(muhaqqiq_high.generate_content_config.thinking_config.thinking_level)

    mudabbir_high = get_mudabbir_agent(thinking_level="HIGH")
    assert "HIGH" in str(mudabbir_high.generate_content_config.thinking_config.thinking_level)


def test_analytical_model_rejects_stale_default():
    """Ensure no agent factory hardcodes gemini-3.5-flash."""
    import ast
    from pathlib import Path

    agents_dir = Path(__file__).parent.parent / "agents"
    for py_file in agents_dir.glob("*.py"):
        source = py_file.read_text()
        if "gemini-3.5-flash" in source:
            pytest.fail(
                f"{py_file.name} still contains 'gemini-3.5-flash' — "
                f"must use 'gemini-3-flash-preview'"
            )
