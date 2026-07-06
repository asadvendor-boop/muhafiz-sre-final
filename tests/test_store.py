"""
tests/test_store.py – Unit Tests for IncidentStore
=========================================================

Tests the async SQLite-backed incident store including:
    - Incident CRUD and state machine transitions
    - Hash-chained event append and integrity verification
    - Approval contract lifecycle (issue → approve → consume)
    - Pipeline run management
    - Agent atomic commit
    - Finalization + seal
"""

import os
import tempfile

import pytest
import pytest_asyncio

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def store():
    """Create a fresh IncidentStore with a temp database."""
    from gateway.store import IncidentStore
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        s = IncidentStore(db_path)
        await s.initialize()
        yield s


@pytest_asyncio.fixture
async def incident_id(store):
    """Create a test incident and return its ID."""
    from gateway.models import Alert, Severity

    alert = Alert(
        severity=Severity.P1,
        service_id="auth-service",
        summary="Test incident",
        alert_type="error_rate",
        error_message="Test error",
    )

    _inc = await store.create_incident(
        incident_id="INC-TEST-001",
        alert=alert,
        scenario_id="test",
    )
    return "INC-TEST-001"


@pytest_asyncio.fixture
async def run_id(store, incident_id):
    """Claim a pipeline run and return its ID."""
    run = await store.claim_pipeline_run(
        incident_id=incident_id,
        phase="phase1",
        revision=1,
        start_stage="triage",
        input_data={"test": True},
    )
    return run["run_id"]


# ── Test: Incident CRUD ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_incident(store):
    """Test creating an incident persists it correctly."""
    from gateway.models import Alert, Severity

    alert = Alert(
        severity=Severity.P2,
        service_id="payment-gateway",
        summary="Payment errors spiking",
        error_message="Stripe timeout",
    )

    inc = await store.create_incident(
        incident_id="INC-CRUD-001",
        alert=alert,
        scenario_id="test_crud",
    )

    assert inc is not None
    assert inc["incident_id"] == "INC-CRUD-001"
    assert inc["status"] == "DETECTED"


@pytest.mark.asyncio
async def test_get_incident(store, incident_id):
    """Test retrieving an incident by ID."""
    inc = await store.get_incident(incident_id)
    assert inc is not None
    assert inc["incident_id"] == incident_id
    assert inc["status"] == "DETECTED"


@pytest.mark.asyncio
async def test_get_nonexistent_incident(store):
    """Test that a missing incident returns None."""
    result = await store.get_incident("INC-DOES-NOT-EXIST")
    assert result is None


@pytest.mark.asyncio
async def test_list_incidents(store, incident_id):
    """Test listing incidents returns all created incidents."""
    incidents = await store.list_incidents()
    assert len(incidents) >= 1
    ids = [i["incident_id"] for i in incidents]
    assert incident_id in ids


# ── Test: State Machine Transitions ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_transition_incident_valid(store, incident_id):
    """Test valid incident state transition."""
    await store.transition_incident(
        incident_id=incident_id,
        from_status="DETECTED",
        to_status="ANALYZING",
    )
    inc = await store.get_incident(incident_id)
    assert inc["status"] == "ANALYZING"


@pytest.mark.asyncio
async def test_transition_incident_invalid_from(store, incident_id):
    """Test transition from wrong state is a no-op (status stays DETECTED)."""
    await store.transition_incident(
        incident_id=incident_id,
        from_status="EXECUTING",  # Wrong - it's DETECTED
        to_status="VERIFYING",
    )
    # Status should remain DETECTED since from_status didn't match
    inc = await store.get_incident(incident_id)
    assert inc["status"] == "DETECTED"


# ── Test: Hash-Chained Events ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_append_event(store, incident_id, run_id):
    """Test appending an event to the chain."""
    event = await store.append_event(
        incident_id=incident_id,
        run_id=run_id,
        actor="test",
        actor_role="tester",
        event_type="test_event",
        summary="Test event appended",
        payload={"key": "value"},
    )

    assert event is not None
    assert event["event_hash"] != ""
    assert event["sequence"] == 1


@pytest.mark.asyncio
async def test_event_chain_grows(store, incident_id, run_id):
    """Test that appending multiple events grows the chain."""
    for i in range(3):
        await store.append_event(
            incident_id=incident_id,
            run_id=run_id,
            actor="test",
            actor_role="tester",
            event_type=f"event_{i}",
            summary=f"Event {i}",
            payload={"index": i},
        )

    events = await store.get_events(incident_id)
    assert len(events) == 3
    assert events[0]["sequence"] < events[1]["sequence"] < events[2]["sequence"]


@pytest.mark.asyncio
async def test_event_hash_chain(store, incident_id, run_id):
    """Test that events form a proper hash chain."""
    await store.append_event(
        incident_id=incident_id,
        run_id=run_id,
        actor="test",
        actor_role="tester",
        event_type="first",
        summary="First event",
        payload={},
    )
    await store.append_event(
        incident_id=incident_id,
        run_id=run_id,
        actor="test",
        actor_role="tester",
        event_type="second",
        summary="Second event",
        payload={},
    )

    events = await store.get_events(incident_id)
    # Second event's previous_hash should be first event's event_hash
    assert events[1]["previous_hash"] == events[0]["event_hash"]


@pytest.mark.asyncio
async def test_verify_chain_valid(store, incident_id, run_id):
    """Test chain verification passes for valid chain."""
    for i in range(5):
        await store.append_event(
            incident_id=incident_id,
            run_id=run_id,
            actor="test",
            actor_role="tester",
            event_type=f"event_{i}",
            summary=f"Event {i}",
            payload={"i": i},
        )

    is_valid = await store.verify_incident_chain(incident_id)
    assert is_valid is True


# ── Test: Pipeline Runs ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_claim_pipeline_run(store, incident_id):
    """Test claiming a pipeline run."""
    run = await store.claim_pipeline_run(
        incident_id=incident_id,
        phase="phase1",
        revision=1,
        start_stage="triage",
        input_data={"test": True},
    )

    assert run is not None
    assert "run_id" in run
    assert run["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_complete_pipeline_run(store, incident_id, run_id):
    """Test completing a pipeline run."""
    await store.complete_pipeline_run(run_id)
    run = await store.get_pipeline_run(run_id)
    assert run["status"] == "COMPLETED"


# ── Test: Approval Contracts ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_issue_contract(store, incident_id):
    """Test issuing an approval contract."""
    contract = await store.issue_contract(
        incident_id=incident_id,
        revision=1,
        plan_id="PLAN-001",
        plan_hash="abc123hash",
        plan_event_hash="event_hash_001",
        canonical_plan_json='{"test":"plan"}',
        actions_json='[]',
        approval_nonce="nonce123",
        token_digest="digest123",
        expires_at="2099-01-01T00:00:00Z",
    )

    assert contract is not None
    assert contract["contract_id"].startswith("CON-")
    assert contract["status"] == "ISSUED"


@pytest.mark.asyncio
async def test_get_active_contract(store, incident_id):
    """Test retrieving the active contract for an incident."""
    await store.issue_contract(
        incident_id=incident_id,
        revision=1,
        plan_id="PLAN-002",
        plan_hash="hash002",
        plan_event_hash="evt_hash_002",
        canonical_plan_json='{}',
        actions_json='[]',
        approval_nonce="nonce002",
        token_digest="digest002",
        expires_at="2099-01-01T00:00:00Z",
    )

    contract = await store.get_active_contract(incident_id)
    assert contract is not None
    assert contract["incident_id"] == incident_id
    assert contract["status"] == "ISSUED"


@pytest.mark.asyncio
async def test_transition_contract(store, incident_id):
    """Test transitioning a contract from ISSUED to APPROVED."""
    await store.issue_contract(
        incident_id=incident_id,
        revision=1,
        plan_id="PLAN-003",
        plan_hash="hash003",
        plan_event_hash="evt_hash_003",
        canonical_plan_json='{}',
        actions_json='[]',
        approval_nonce="nonce003",
        token_digest="digest003",
        expires_at="2099-01-01T00:00:00Z",
    )

    await store.transition_contract(
        incident_id=incident_id,
        revision=1,
        from_status="ISSUED",
        to_status="APPROVED",
    )

    contract = await store.get_active_contract(incident_id)
    assert contract["status"] == "APPROVED"


# ── Test: Atomic Agent Commit ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_commit_agent_decision(store, incident_id, run_id):
    """Test the atomic agent commit pattern."""
    event = await store.commit_agent_decision(
        incident_id=incident_id,
        run_id=run_id,
        actor="nigehban",
        actor_role="triage",
        event_type="triage_completed",
        summary="Triage: P1 - Test incident",
        payload={"severity": "P1", "is_actionable": True},
        new_incident_status="ANALYZING",
    )

    assert event is not None
    assert event["event_hash"] != ""

    # Verify incident status was updated
    inc = await store.get_incident(incident_id)
    assert inc["status"] == "ANALYZING"


# ── Test: Audit Proof ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_proof(store, incident_id, run_id):
    """Test generating an audit proof."""
    # Append some events
    await store.append_event(
        incident_id=incident_id,
        run_id=run_id,
        actor="test",
        actor_role="tester",
        event_type="audit_test",
        summary="Audit test event",
        payload={},
    )

    proof = await store.get_audit_proof(incident_id)
    assert proof is not None
    assert "chain_valid" in proof
    assert "event_hashes" in proof
