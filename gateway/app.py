"""
gateway/app.py – FastAPI Gateway for MuhafizSRE
======================================================

Incident Command Room API with:
    - Persistent SQLite-backed incident store
    - Hash-chained event timeline via SSE
    - HMAC-SHA256 approval contracts
    - Phase 1 pipeline orchestration endpoint
    - Chain integrity verification

Endpoint Map:
    ┌──────────────────────────────────────────────────────────────────┐
    │ Method │ Path                                │ Purpose            │
    ├────────┼─────────────────────────────────────┼────────────────────┤
    │ GET    │ /health                             │ Liveness probe     │
    │ POST   │ /api/incidents                      │ Create incident    │
    │ GET    │ /api/incidents                      │ List incidents     │
    │ GET    │ /api/incidents/{id}                 │ Get incident       │
    │ GET    │ /api/incidents/{id}/events          │ SSE event stream   │
    │ GET    │ /api/incidents/{id}/contract        │ Active contract    │
    │ POST   │ /api/incidents/{id}/decisions       │ Human decision     │
    │ GET    │ /api/incidents/{id}/audit           │ Audit proof        │
    │ GET    │ /api/incidents/{id}/chain/verify    │ Chain verification │
    └──────────────────────────────────────────────────────────────────┘

Security:
    - CORS origins configurable via MUHAFIZ_CORS_ORIGINS
    - Approval tokens are HMAC-SHA256 signed, never persisted
    - All decisions recorded as hash-chained events
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware

from gateway.models import (
    Alert,
    CreateIncidentRequest,
    CreateIncidentResponse,
    DecisionAction,
    DecisionRequest,
    IncidentStatus,
    _utc_now,
    canonical_json,
    sha256_hex,
)
from gateway.security import (
    ApprovalTokenManager,
    Settings,
    build_token_claims,
    generate_approval_nonce,
    validate_decision_request,
)
from gateway.store import IncidentStore

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("MUHAFIZ_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application state (initialised in lifespan)
# ---------------------------------------------------------------------------
from shared.dependencies import (  # noqa: E402
    init_dependencies,
    get_store as _get_store,
    get_token_manager as _get_token_manager,
    get_settings as _get_settings,
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup: init store + settings + DI container. Shutdown: log."""

    # Load settings
    settings = Settings.from_env()

    # In test mode, generate a throwaway secret if none/too-short provided
    if settings.test_mode and (
        not settings.approval_secret or len(settings.approval_secret) < 32
    ):
        import secrets as _s
        settings.approval_secret = _s.token_hex(32)
        logger.warning("TEST MODE: generated throwaway approval secret")

    try:
        settings.validate()
    except ValueError as exc:
        raise

    # Init token manager
    token_manager = ApprovalTokenManager(settings.approval_secret)

    # Init store
    store = IncidentStore(settings.db_path)
    await store.initialize()

    # Initialize the application-level dependency container.
    # Agents access these via shared.dependencies.get_store() etc.
    init_dependencies(store=store, token_manager=token_manager, settings=settings)

    logger.info("=" * 60)
    logger.info("  MuhafizSRE Gateway v%s starting up", application.version)
    logger.info("  Database: %s", settings.db_path)
    logger.info("  Test mode: %s", settings.test_mode)
    logger.info("  Default model: %s", settings.default_model)
    logger.info("  Speed model:      %s", settings.speed_model)
    logger.info("  Analytical model: %s", settings.analytical_model)
    logger.info("  Safety model:     %s", settings.safety_model)
    logger.info("=" * 60)

    # ── Model guard ──────────────────────────────────────────────────
    # Fail loudly if the analytical model is still gemini-3.5-flash.
    # The validated default is gemini-3-flash-preview (21/21 benchmark).
    if "gemini-3.5-flash" in settings.analytical_model:
        raise RuntimeError(
            f"STARTUP BLOCKED: analytical_model={settings.analytical_model!r} "
            f"is the stale default. Set MUHAFIZ_ANALYTICAL_MODEL=gemini-3-flash-preview "
            f"or update gateway/security.py."
        )

    # ── Pipeline Supervisor ───────────────────────────────────────────
    from gateway.pipeline_supervisor import PipelineSupervisor
    max_tasks = int(os.environ.get("MUHAFIZ_MAX_PIPELINE_TASKS", "2"))
    stale_after = int(os.environ.get("MUHAFIZ_STALE_RUN_AFTER_SECONDS", "600"))
    supervisor = PipelineSupervisor(
        store=store, max_concurrent=max_tasks, stale_after_seconds=stale_after
    )
    application.state.pipeline_supervisor = supervisor
    await supervisor.recover_stale_runs()

    yield

    await application.state.pipeline_supervisor.shutdown()
    logger.info("MuhafizSRE Gateway shutting down gracefully.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="MuhafizSRE Gateway",
    description=(
        "Incident Command Room — Security gateway for human-governed "
        "autonomous incident response with hash-chain audit trail."
    ),
    version="5.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS
_cors_origins_raw = os.environ.get(
    "MUHAFIZ_CORS_ORIGINS",
    "http://localhost:3000,http://localhost:5173,http://localhost:8080",
)
_cors_origins = [origin.strip() for origin in _cors_origins_raw.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Health Check
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Infrastructure"])
async def health_check():
    """Liveness/readiness probe."""
    store = _get_store()
    incidents = await store.list_incidents()
    return {
        "status": "healthy",
        "service": "muhafiz-gateway",
        "version": app.version,
        "incident_count": len(incidents),
        "timestamp": _utc_now(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 2. Incident CRUD
# ═══════════════════════════════════════════════════════════════════════════

@app.post(
    "/api/incidents",
    tags=["Incidents"],
    status_code=status.HTTP_201_CREATED,
)
async def create_incident(request: CreateIncidentRequest):
    """
    Create a new incident and launch Phase 1 pipeline.

    Returns incident_id, run_id, status, and events SSE URL.
    """
    store = _get_store()

    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"

    # Persist incident
    await store.create_incident(
        incident_id=incident_id,
        alert=request.alert,
        scenario_id=request.scenario_id,
    )

    # Claim a Phase 1 pipeline run
    run = await store.claim_pipeline_run(
        incident_id=incident_id,
        phase="phase1",
        revision=1,
        start_stage="triage",
        input_data={"alert": request.alert.model_dump(mode="json")},
    )
    run_id = run["run_id"]

    # Append incident_created event
    await store.append_event(
        incident_id=incident_id,
        run_id=run_id,
        actor="gateway",
        actor_role="gateway",
        event_type="incident_created",
        summary=f"Incident created: {request.alert.summary}",
        payload=request.alert.model_dump(mode="json"),
    )

    logger.info(
        "Incident created — id=%s, severity=%s, service=%s, run=%s",
        incident_id,
        request.alert.severity.value,
        request.alert.service_id,
        run_id,
    )

    # Launch Phase 1 pipeline via supervised runner
    await app.state.pipeline_supervisor.submit_phase1(
        incident_id=incident_id,
        run_id=run_id,
        alert=request.alert,
        pipeline_fn=_run_phase1_pipeline,
    )

    return CreateIncidentResponse(
        incident_id=incident_id,
        run_id=run_id,
        status=IncidentStatus.DETECTED,
        events_url=f"/api/incidents/{incident_id}/events",
    )


@app.get("/api/incidents", tags=["Incidents"])
async def list_incidents():
    """List all incidents (newest first)."""
    store = _get_store()
    incidents = await store.list_incidents()
    return incidents


@app.get("/api/incidents/{incident_id}", tags=["Incidents"])
async def get_incident(incident_id: str):
    """Get a single incident by ID."""
    store = _get_store()
    incident = await store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found.",
        )
    return incident


# ═══════════════════════════════════════════════════════════════════════════
# 3. SSE Event Stream
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/incidents/{incident_id}/events", tags=["Events"])
async def stream_events(incident_id: str, request: Request):
    """
    Server-Sent Events stream for incident timeline.

    Supports Last-Event-ID for reconnection.
    Polls store every 1s for new events.
    """
    from sse_starlette.sse import EventSourceResponse

    store = _get_store()

    # Verify incident exists
    incident = await store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found.",
        )

    # Get Last-Event-ID for reconnection (prefer query param, fallback to header)
    last_event_id = request.query_params.get("last_event_id") or request.headers.get("Last-Event-ID", "0")
    try:
        after_sequence = int(last_event_id)
    except (ValueError, TypeError):
        after_sequence = 0

    async def event_generator():
        """Yield events and room messages as they appear in the store."""
        event_seq = after_sequence
        room_seq = 0  # Track room message sequence separately
        terminal_states = {
            "RESOLVED", "FALSE_ALARM", "BLOCKED", "ESCALATED",
            "REJECTED", "PIPELINE_FAILED", "EXECUTION_FAILED",
            "RECOVERY_FAILED", "DEGRADED",
        }
        heartbeat_interval = 0
        while True:
            if await request.is_disconnected():
                break

            # Fetch new events (unnamed — no 'event' key)
            events = await store.get_events(incident_id, after=event_seq)
            for event in events:
                event_seq = event["sequence"]
                yield {
                    "id": str(event_seq),
                    "data": json.dumps({
                        "type": "event",
                        "sequence": event["sequence"],
                        "event_type": event["event_type"],
                        "actor": event["actor"],
                        "actor_role": event["actor_role"],
                        "summary": event["summary"],
                        "payload": json.loads(
                            event.get("payload_json", "{}")
                        ),
                        "event_hash": event["event_hash"],
                        "previous_hash": event["previous_hash"],
                        "created_at": event["created_at"],
                    }),
                }

            # Fetch new room messages
            room_msgs = await store.get_room_messages_since(
                incident_id, after_seq=room_seq,
            )
            for msg in room_msgs:
                room_seq = msg.get("sequence", room_seq)
                yield {
                    "data": json.dumps({
                        "type": "room_message",
                        **msg,
                    }),
                }

            # Check terminal state
            current_incident = await store.get_incident(incident_id)
            if current_incident and current_incident.get("status") in terminal_states:
                yield {
                    "data": json.dumps({
                        "type": "stream_complete",
                        "final_status": current_incident.get("status"),
                        "final_event_hash": current_incident.get("final_event_hash", ""),
                    }),
                }
                break

            heartbeat_interval += 1
            if heartbeat_interval >= 15:
                yield {"data": json.dumps({"type": "heartbeat", "ts": _utc_now()})}
                heartbeat_interval = 0

            await asyncio.sleep(1.0)

    return EventSourceResponse(event_generator())


# ═══════════════════════════════════════════════════════════════════════════
# 3b. REST Event List (polling fallback)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/incidents/{incident_id}/events/list", tags=["Events"])
async def list_events(incident_id: str):
    """REST endpoint returning all events for an incident (polling fallback)."""
    store = _get_store()
    incident = await store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found.",
        )
    raw_events = await store.get_events(incident_id)
    return {
        "events": [
            {
                "type": "event",
                "incident_id": incident_id,
                "sequence": e["sequence"],
                "event_type": e["event_type"],
                "actor": e["actor"],
                "actor_role": e["actor_role"],
                "summary": e["summary"],
                "payload": json.loads(e.get("payload_json", "{}")),
                "event_hash": e["event_hash"],
                "previous_hash": e["previous_hash"],
                "created_at": e["created_at"],
            }
            for e in raw_events
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
# 4. Active Contract
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/incidents/{incident_id}/contract", tags=["Approval"])
async def get_active_contract(incident_id: str):
    """
    Get the active approval contract for an incident.

    Returns the contract with the reconstructed approval token.
    The token is NEVER persisted — only reconstructed on demand.
    """
    store = _get_store()
    token_mgr = _get_token_manager()

    incident = await store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found.",
        )

    contract = await store.get_active_contract(incident_id)
    if contract is None:
        return {"contract": None, "message": "No active contract."}

    # Read immutable claims from stored contract — fail closed
    stored_claims = contract.get("claims_json", "")
    if not stored_claims:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Contract missing immutable claims. Re-issue required.",
        )
    claims = json.loads(stored_claims)
    token = token_mgr.reconstruct_token(claims)

    # Build plan from stored JSON
    plan_data = json.loads(contract.get("canonical_plan_json", "{}"))
    actions_data = json.loads(contract.get("actions_json", "[]"))

    return {
        "contract": {
            "contract_id": contract["contract_id"],
            "incident_id": contract["incident_id"],
            "revision": contract["revision"],
            "status": contract["status"],
            "plan_id": contract["plan_id"],
            "plan_hash": contract["plan_hash"],
            "actions": actions_data,
            "plan": plan_data,
            "created_at": contract["created_at"],
            "expires_at": contract["expires_at"],
        },
        "approval_token": token,
        "token_expires_at": contract["expires_at"],
    }


# ═══════════════════════════════════════════════════════════════════════════
# 5. Human Decision
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/incidents/{incident_id}/decisions", tags=["Approval"])
async def submit_decision(incident_id: str, request: DecisionRequest):
    """
    Submit a human decision for an incident (§17.3).

    Actions:
        APPROVE           → Execute remediation plan
        REJECT            → Close incident as rejected
        REQUEST_REVISION  → Send plan back for revision
        MARK_FALSE_ALARM  → Close as false alarm
    """
    store = _get_store()
    token_mgr = _get_token_manager()

    # Validate incident exists
    incident = await store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found.",
        )

    # Get contract
    contract = await store.get_contract_by_id(request.contract_id)
    if contract is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contract '{request.contract_id}' not found.",
        )

    # ── Universal decision guards (all actions) ─────────────────────
    # Prevent stale, cross-incident, or wrong-state decisions.
    if contract.get("incident_id") != incident_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Contract does not belong to this incident.",
        )
    if contract.get("revision") != request.revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Revision mismatch: contract has {contract.get('revision')}, "
                f"request has {request.revision}."
            ),
        )
    if contract.get("status") != "ISSUED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Contract is not actionable (status={contract.get('status')}).",
        )
    if incident.get("status") != IncidentStatus.AWAITING_APPROVAL.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Incident is not awaiting approval "
                f"(status={incident.get('status')})."
            ),
        )

    # For APPROVE, validate the token
    if request.action == DecisionAction.APPROVE:
        if not request.approval_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Approval token required for APPROVE action.",
            )

        stored_claims = contract.get("claims_json", "")
        if not stored_claims:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Contract missing immutable claims. Cannot validate token.",
            )
        claims = json.loads(stored_claims)

        valid, error_msg = validate_decision_request(
            token_manager=token_mgr,
            token=request.approval_token,
            claims=claims,
            contract=contract,
            incident=incident,
        )

        if not valid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Token validation failed: {error_msg}",
            )

    # Record the decision event
    decision_payload = {
        "action": request.action.value,
        "contract_id": request.contract_id,
        "revision": request.revision,
        "operator": request.operator_label,
        "feedback": request.feedback,
    }

    run_id = incident.get("active_run_id", "UNKNOWN")

    # Process by action type
    if request.action == DecisionAction.APPROVE:
        # ── Atomic single-winner claim ─────────────────────────────────
        # claim_approval performs read-check + write in one BEGIN IMMEDIATE
        # transaction so that exactly one of N concurrent requests wins.
        claimed, claim_reason, event = await store.claim_approval(
            incident_id=incident_id,
            contract_id=request.contract_id,
            revision=request.revision,
            approved_by=request.operator_label,
            run_id=run_id,
            decision_payload=decision_payload,
        )

        if not claimed:
            # Another request already claimed this approval
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Approval already claimed or incident in wrong state: "
                    f"{claim_reason}"
                ),
            )

        # Launch Phase 2 (execution) via supervised runner
        await app.state.pipeline_supervisor.submit_phase2(
            incident_id=incident_id,
            run_id=run_id,
            contract=contract,
            pipeline_fn=_run_phase2_execution,
        )

        return {
            "status": "approved",
            "event_hash": event["event_hash"],
            "message": "Plan approved. Execution started.",
        }

    elif request.action == DecisionAction.REJECT:
        await store.transition_contract(
            incident_id=incident_id,
            revision=request.revision,
            from_status="ISSUED",
            to_status="REJECTED",
        )

        event = await store.commit_agent_decision(
            incident_id=incident_id,
            run_id=run_id,
            actor=request.operator_label,
            actor_role="human",
            event_type="human_rejected",
            summary=f"Plan rejected by {request.operator_label}",
            payload=decision_payload,
            new_incident_status="REJECTED",
        )

        return {
            "status": "rejected",
            "event_hash": event["event_hash"],
            "message": "Plan rejected. Incident closed.",
        }

    elif request.action == DecisionAction.REQUEST_REVISION:
        await store.invalidate_active_contracts(incident_id)

        event = await store.commit_agent_decision(
            incident_id=incident_id,
            run_id=run_id,
            actor=request.operator_label,
            actor_role="human",
            event_type="revision_requested",
            summary=f"Revision requested by {request.operator_label}",
            payload=decision_payload,
            new_incident_status="REVISION_REQUESTED",
        )

        # Launch revision pipeline via supervised runner
        await app.state.pipeline_supervisor.submit_revision(
            incident_id=incident_id,
            run_id=run_id,
            feedback=request.feedback or "",
            pipeline_fn=_run_revision_pipeline,
        )

        return {
            "status": "revision_requested",
            "event_hash": event["event_hash"],
            "message": "Revision requested. Pipeline restarting.",
        }

    elif request.action == DecisionAction.MARK_FALSE_ALARM:
        await store.invalidate_active_contracts(incident_id)

        event = await store.commit_agent_decision(
            incident_id=incident_id,
            run_id=run_id,
            actor=request.operator_label,
            actor_role="human",
            event_type="human_false_alarm",
            summary=f"Marked false alarm by {request.operator_label}",
            payload=decision_payload,
            new_incident_status="FALSE_ALARM",
        )

        return {
            "status": "false_alarm",
            "event_hash": event["event_hash"],
            "message": "Incident marked as false alarm.",
        }


# ═══════════════════════════════════════════════════════════════════════════
# 6. Audit Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/incidents/{incident_id}/audit", tags=["Audit"])
async def get_audit_proof(incident_id: str):
    """Get the complete audit proof for an incident."""
    store = _get_store()
    incident = await store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found.",
        )
    return await store.get_audit_proof(incident_id)


@app.get("/api/incidents/{incident_id}/chain/verify", tags=["Audit"])
async def verify_incident_chain(incident_id: str):
    """Verify the hash chain integrity for an incident."""
    store = _get_store()
    incident = await store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found.",
        )
    is_valid = await store.verify_incident_chain(incident_id)
    return {
        "chain_valid": is_valid,
        "incident_id": incident_id,
        "verified_at": _utc_now(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 6B. Room Messages
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/incidents/{incident_id}/room", tags=["Room"])
async def get_room_messages(incident_id: str):
    """Get all agent room messages for an incident."""
    store = _get_store()
    incident = await store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found.",
        )
    messages = await store.get_room_messages(incident_id)
    return {"messages": messages, "count": len(messages)}


# ═══════════════════════════════════════════════════════════════════════════
# 7. Phase 1 Pipeline (background task)
# ═══════════════════════════════════════════════════════════════════════════

async def _run_single_agent(
    agent_name: str, state: dict, message: str,
    thinking_level: str | None = None,
) -> dict:
    """Run one agent via ADK Runner, return updated session state.

    Args:
        agent_name: Name of the agent to run (must be in AGENT_FACTORIES
            or AGENT_REGISTRY).
        state: Serializable session state dict.
        message: User message to send to the agent.
        thinking_level: Optional override for the agent's thinking level.
            When provided, a fresh agent is created via the factory with
            this thinking level. When None, the factory default is used.

    NOTE: `state` contains ONLY serializable data (incident_id, run_id,
    plan dicts, verdicts, etc). NO live objects (store, token_manager).
    Agents access store via shared.dependencies.get_store().
    """
    from agents.agent import AGENT_REGISTRY, AGENT_FACTORIES
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    # Always prefer factory (creates fresh agent with current context).
    # Pass thinking_level if provided, otherwise let factory use default.
    factory = AGENT_FACTORIES.get(agent_name)
    if factory:
        scenario_id = os.environ.get("MUHAFIZ_SCENARIO_ID", "")
        # Muhaqqiq factory takes (scenario_id, thinking_level);
        # other factories take (thinking_level) only.
        if agent_name == "muhaqqiq":
            if thinking_level is not None:
                agent = factory(scenario_id, thinking_level=thinking_level)
            else:
                agent = factory(scenario_id)
        else:
            if thinking_level is not None:
                agent = factory(thinking_level=thinking_level)
            else:
                agent = factory()
    else:
        agent = AGENT_REGISTRY[agent_name]

    # Extract model and thinking config for telemetry logging
    agent_model = getattr(agent, "model", "unknown")
    agent_config = getattr(agent, "generate_content_config", None)
    effective_thinking = "default"
    if agent_config:
        tc = getattr(agent_config, "thinking_config", None)
        if tc:
            effective_thinking = getattr(tc, "thinking_level", "default")

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="muhafizsre", user_id="gateway", state=state,
    )
    runner = Runner(
        agent=agent, app_name="muhafizsre",
        session_service=session_service,
    )

    incident_id = state.get("incident_id", "?")
    logger.info(
        "[ADK:%s] Starting agent for incident=%s, model=%s, thinking=%s, input_state_keys=%s",
        agent_name, incident_id, agent_model, effective_thinking,
        sorted(state.keys()),
    )

    # ── Runtime model guard ──────────────────────────────────────────
    if agent_name in ("muhaqqiq", "mudabbir") and "gemini-3.5-flash" in str(agent_model):
        logger.error(
            "MODEL MISMATCH: %s is using %s instead of gemini-3-flash-preview. "
            "Check MUHAFIZ_ANALYTICAL_MODEL env var.",
            agent_name, agent_model,
        )

    event_count = 0
    tools_used: set[str] = set()
    tools_succeeded: set[str] = set()
    tools_failed: set[str] = set()
    # ── Token usage tracking ───────────────────────────────────────
    invocation_usage = {
        "prompt_tokens": 0,
        "candidates_tokens": 0,
        "thoughts_tokens": 0,
        "total_tokens": 0,
    }
    try:
        async for event in runner.run_async(
            user_id="gateway", session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=message)],
            ),
        ):
            event_count += 1
            # ── Accumulate token usage from each event ─────────────────
            um = getattr(event, "usage_metadata", None)
            if um:
                invocation_usage["prompt_tokens"] += getattr(um, "prompt_token_count", 0) or 0
                invocation_usage["candidates_tokens"] += getattr(um, "candidates_token_count", 0) or 0
                invocation_usage["thoughts_tokens"] += getattr(um, "thoughts_token_count", 0) or 0
                invocation_usage["total_tokens"] += getattr(um, "total_token_count", 0) or 0

            # Log function calls (tool invocations by the model)
            # Use ADK's get_function_calls() — the documented event API
            for fc in event.get_function_calls():
                fn_name = fc.name
                fn_args = fc.args or {}
                args_str = str(fn_args)[:500]
                logger.info(
                    "[ADK:%s] FUNCTION_CALL: %s(%s)",
                    agent_name, fn_name, args_str,
                )
                tools_used.add(fn_name)
            # Log function responses (tool results)
            for fr in event.get_function_responses():
                fn_name = fr.name
                response = fr.response or {}
                resp_str = str(response)[:500]
                # Detect tool errors: ADK wraps MCP errors as
                # {"content": [{"text": "Error ..."}], "isError": True}
                is_error = False
                if isinstance(response, dict):
                    is_error = response.get("isError", False)
                    # Also check nested content list
                    for item in response.get("content", []):
                        if isinstance(item, dict) and item.get("isError"):
                            is_error = True
                if is_error:
                    logger.warning(
                        "[ADK:%s] TOOL_ERROR: %s → %s",
                        agent_name, fn_name, resp_str,
                    )
                    tools_failed.add(fn_name)
                else:
                    tools_succeeded.add(fn_name)
                logger.info(
                    "[ADK:%s] FUNCTION_RESPONSE: %s → %s",
                    agent_name, fn_name, resp_str,
                )
            # Log errors
            if hasattr(event, "error") and event.error:
                logger.error(
                    "[ADK:%s] ERROR: %s", agent_name, event.error,
                )
            # Log text content from model
            if hasattr(event, "content") and event.content:
                parts = getattr(event.content, "parts", []) or []
                for part in parts:
                    if hasattr(part, "text") and part.text:
                        text_preview = part.text[:300].replace("\n", " ")
                        logger.info(
                            "[ADK:%s] MODEL_TEXT: %s", agent_name, text_preview,
                        )

        # ── Log token usage summary ────────────────────────────────────
        logger.info(
            "[ADK:%s] TOKEN_USAGE: prompt=%d, candidates=%d, thoughts=%d, total=%d",
            agent_name,
            invocation_usage["prompt_tokens"],
            invocation_usage["candidates_tokens"],
            invocation_usage["thoughts_tokens"],
            invocation_usage["total_tokens"],
        )

        updated = await session_service.get_session(
            app_name="muhafizsre", user_id="gateway",
            session_id=session.id,
        )
        final_state = updated.state if updated else state
        # Merge tracked tool calls into state for evaluator (Fix #5)
        existing_tools = set(final_state.get("tools_used", []))
        existing_tools.update(tools_used)
        final_state["tools_used"] = sorted(existing_tools)

        existing_succeeded = set(final_state.get("tools_succeeded", []))
        existing_succeeded.update(tools_succeeded)
        final_state["tools_succeeded"] = sorted(existing_succeeded)

        existing_failed = set(final_state.get("tools_failed", []))
        existing_failed.update(tools_failed)
        final_state["tools_failed"] = sorted(existing_failed)

        # ── Persist per-invocation usage in session state ──────────────
        # Append to a list so the evaluator can see token usage per agent
        # invocation across the entire pipeline run.
        usage_entry = {
            "agent": agent_name,
            "model": str(agent_model),
            "thinking_level": str(effective_thinking),
            "event_count": event_count,
            **invocation_usage,
        }
        usage_log = final_state.get("invocation_usage_log", [])
        usage_log.append(usage_entry)
        final_state["invocation_usage_log"] = usage_log

        logger.info(
            "[ADK:%s] Completed: %d events, model=%s, thinking=%s, "
            "tokens=%d, final_state_keys=%s, tools_used=%s",
            agent_name, event_count, agent_model, effective_thinking,
            invocation_usage["total_tokens"],
            sorted(final_state.keys()), sorted(tools_used),
        )
        return final_state

    finally:
        # ── Persist to durable store (survives exceptions + phase
        #    boundaries).  Inside `finally` so partial token counts
        #    are captured even when the agent raises mid-run. ──────
        telemetry_payload = {
            "agent": agent_name,
            "model": str(agent_model),
            "thinking_level": str(effective_thinking),
            "event_count": event_count,
            **invocation_usage,
            # ── Per-invocation tool outcomes ──────────────────────────
            "tools_called": sorted(tools_used),
            "tools_succeeded": sorted(tools_succeeded),
            "tools_failed": sorted(tools_failed),
            "tools_unresolved": sorted(
                tools_used - tools_succeeded - tools_failed
            ),
        }
        run_id = state.get("run_id", "")
        if run_id and incident_id != "?":
            try:
                from shared.dependencies import get_store
                s = get_store()
                await s.append_event(
                    incident_id=incident_id,
                    run_id=run_id,
                    actor=agent_name,
                    actor_role="system",
                    event_type="agent_usage_telemetry",
                    summary=(
                        f"Token usage: {invocation_usage['total_tokens']} total "
                        f"(prompt={invocation_usage['prompt_tokens']}, "
                        f"candidates={invocation_usage['candidates_tokens']}, "
                        f"thoughts={invocation_usage['thoughts_tokens']})"
                    ),
                    payload=telemetry_payload,
                )
            except Exception as e:
                logger.warning(
                    "[ADK:%s] Failed to persist usage telemetry: %s",
                    agent_name, e,
                )


async def _run_phase1_pipeline(
    incident_id: str,
    run_id: str,
    alert: Alert,
) -> None:
    """
    Explicit routed Phase 1: Nigehban → [false_alarm?] → Muhaqqiq → Mudabbir → Muhtasib.

    Supports:
    - False alarm short-circuit after triage
    - Challenge routing (EVIDENCE → Muhaqqiq, PLAN → Mudabbir)
    - Up to 3 challenge rounds before escalation
    """
    store = _get_store()
    settings = _get_settings()
    token_mgr = _get_token_manager()
    MAX_CHALLENGE_ROUNDS = 3

    try:
        logger.info("Phase 1 starting: incident=%s, run=%s", incident_id, run_id)

        alert_text = (
            f"Process this incident alert:\n"
            f"Service: {alert.service_id}\n"
            f"Severity: {alert.severity.value}\n"
            f"Summary: {alert.summary}\n"
            f"Error: {alert.error_message}\n"
            f"Timestamp: {alert.timestamp}"
        )

        # Shared session state — ONLY serializable data
        state = {
            "incident_id": incident_id,
            "run_id": run_id,
            "alert": alert.model_dump(mode="json"),
        }

        # ── Step 1: Triage (Nigehban) ────────────────────────────────────
        state = await _run_single_agent("nigehban", state, alert_text)
        triage = state.get("triage_result", {})

        if not triage.get("is_actionable", True):
            # SHORT-CIRCUIT: false alarm → terminate immediately
            changed = await store.transition_incident(
                incident_id, "DETECTED", "FALSE_ALARM",
            )
            if changed:
                await store.append_event(
                    incident_id=incident_id, run_id=run_id,
                    actor="nigehban", actor_role="watchman",
                    event_type="false_alarm_detected",
                    summary="Alert classified as false alarm — pipeline terminated.",
                    payload=triage,
                )
            await store.complete_pipeline_run(run_id)
            logger.info("Phase 1 short-circuit: false alarm for %s", incident_id)
            return

        # ── Step 2: Investigation (Muhaqqiq) ─────────────────────────────
        # Include triage context in message so model has full picture
        muhaqqiq_message = (
            f"{alert_text}\n\n"
            f"=== TRIAGE RESULT (from Nigehban) ===\n"
            f"{json.dumps(triage, indent=2, default=str)}"
        )

        # Track investigation provenance for retry logic
        previous_investigation_hash = state.get("investigation_event_hash")
        state["investigation_first_pass_commit"] = True
        state["investigation_retry_used"] = False
        state["investigation_attempt_count"] = 1

        state = await _run_single_agent("muhaqqiq", state, muhaqqiq_message)

        # ── Stage guard: Muhaqqiq MUST have committed investigation ──────
        investigation = state.get("investigation_result")
        if not investigation:
            # ── BOUNDED LLM RETRY ─────────────────────────────────────
            # Muhaqqiq analyzed but did not call commit_investigation.
            # Instead of faking a diagnosis with keyword matching,
            # re-run the agent at HIGH thinking with full evidence context.
            # If the retry also fails, fail the pipeline — no synthetic evidence.
            logger.warning(
                "Muhaqqiq did not commit investigation for %s — "
                "activating bounded evidence-preserving retry (attempt 2/2)",
                incident_id,
            )

            # Preserve retry provenance in state
            state["investigation_first_pass_commit"] = False
            state["investigation_retry_used"] = True
            state["investigation_attempt_count"] = 2

            # Build a full-evidence retry message
            tools_used = state.get("tools_used", [])
            tools_succeeded = [
                t for t in tools_used
                if t not in ("fetch_telemetry", "commit_investigation",
                             "consume_alert", "commit_triage")
            ]
            tools_failed = state.get("tools_failed", [])

            retry_message = (
                f"{alert_text}\n\n"
                f"=== TRIAGE RESULT (from Nigehban) ===\n"
                f"{json.dumps(triage, indent=2, default=str)}\n\n"
                f"=== RETRY INSTRUCTION ===\n"
                f"Your previous investigation pass did NOT call "
                f"commit_investigation. You MUST call commit_investigation "
                f"exactly once with your diagnosis.\n\n"
                f"Tools you called: {tools_succeeded}\n"
                f"Tools that failed: {tools_failed}\n\n"
                f"RULES:\n"
                f"1. You MUST call commit_investigation with your findings.\n"
                f"2. Do NOT invent evidence you did not observe.\n"
                f"3. You MAY re-query MCP tools if prior outputs are "
                f"unavailable.\n"
                f"4. Base your diagnosis on actual tool observations only.\n"
            )

            # Re-run Muhaqqiq at HIGH thinking level
            previous_hash = state.get("investigation_event_hash")
            state = await _run_single_agent(
                "muhaqqiq", state, retry_message,
                thinking_level="HIGH",
            )

            # Check if retry produced a valid commit
            investigation = state.get("investigation_result")
            new_hash = state.get("investigation_event_hash")
            committed = bool(
                investigation
                and new_hash
                and new_hash != previous_hash
            )

            if not committed:
                # ── FAIL CLOSED ──────────────────────────────────────
                # Both attempts failed. No synthetic diagnosis.
                # No keyword matching. No Python heuristics.
                logger.error(
                    "Muhaqqiq failed to commit investigation after "
                    "bounded retry for %s — failing pipeline",
                    incident_id,
                )
                await store.fail_pipeline_once(
                    incident_id=incident_id,
                    run_id=run_id,
                    phase="phase1",
                    error_type="MissingInvestigationCommit",
                    error_message=(
                        "Muhaqqiq failed to commit an investigation "
                        "after one bounded retry (2/2 attempts exhausted). "
                        "No synthetic diagnosis generated."
                    ),
                )
                return

            # Retry succeeded — enrich with provenance metadata
            if isinstance(investigation, dict):
                investigation["first_pass_commit"] = False
                investigation["retry_used"] = True
                investigation["attempt_count"] = 2
                investigation["retry_reason"] = "missing_commit"
                # Explicitly do NOT set fallback_used — a bounded
                # retry is not a synthetic fallback.
                state["investigation_result"] = investigation

            logger.info(
                "Bounded retry succeeded for %s — investigation "
                "committed on attempt 2 (event_hash=%s)",
                incident_id, new_hash,
            )

        # ── Investigation reliability metrics ────────────────────────────
        inv_metrics = {
            "incident_id": incident_id,
            "first_pass_commit": state.get("investigation_first_pass_commit", True),
            "retry_used": state.get("investigation_retry_used", False),
            "attempt_count": state.get("investigation_attempt_count", 1),
        }
        logger.info(
            "[METRICS:investigation] %s",
            json.dumps(inv_metrics, default=str),
        )

        # ── Step 3: Planning (Mudabbir) ──────────────────────────────────
        # Deterministically inject upstream context into the message
        challenge_feedback = state.get("challenge_feedback", "")
        revision = state.get("plan_revision", 1)
        mudabbir_message = (
            f"{alert_text}\n\n"
            f"=== TRIAGE RESULT ===\n"
            f"{json.dumps(triage, indent=2, default=str)}\n\n"
            f"=== INVESTIGATION RESULT (from Muhaqqiq) ===\n"
            f"{json.dumps(investigation, indent=2, default=str)}"
        )
        if challenge_feedback:
            mudabbir_message += (
                f"\n\n=== SAFETY CHALLENGE FEEDBACK (from Muhtasib) ===\n"
                f"Plan revision #{revision} requested.\n"
                f"{challenge_feedback}"
            )
        state = await _run_single_agent("mudabbir", state, mudabbir_message)

        # ── Stage guard: Mudabbir MUST have committed a plan ─────────────
        plan = state.get("plan")
        if not plan:
            logger.error(
                "Mudabbir did not commit plan for %s — aborting pipeline",
                incident_id,
            )
            await store.fail_pipeline_once(
                incident_id=incident_id,
                run_id=run_id,
                phase="phase1",
                error_type="MissingPlanCommit",
                error_message="Mudabbir finished without calling commit_plan.",
            )
            return

        # ── Step 4: Safety Review (Muhtasib) with challenge routing ──────
        # ── Challenge loop ─────────────────────────────────────────────
        # We use a while-loop with an explicit counter.  The counter
        # increments ONLY when a CHALLENGE triggers a revision.  If we
        # receive a CHALLENGE but have already used all allowed rounds,
        # we escalate immediately WITHOUT producing another revision —
        # preventing unreviewed final revisions.
        all_challenge_feedback: list[str] = []   # accumulated across rounds
        challenges_used = 0

        while True:
            # ── Reset retry tracking per safety-review round ──────────
            state["first_pass_commit"] = True
            state["retry_used"] = False

            # Snapshot the verdict hash BEFORE invocation so we can detect
            # whether Muhtasib produced a *new* commit (an older challenge
            # verdict hash may still be present in state).
            prev_verdict_hash = state.get("verdict_event_hash")

            # Deterministically inject ALL upstream context into the message
            muhtasib_message = (
                f"{alert_text}\n\n"
                f"=== TRIAGE RESULT ===\n"
                f"{json.dumps(triage, indent=2, default=str)}\n\n"
                f"=== INVESTIGATION RESULT ===\n"
                f"{json.dumps(investigation, indent=2, default=str)}\n\n"
                f"=== REMEDIATION PLAN (Rev {state.get('plan', {}).get('revision', 1)}) ===\n"
                f"{json.dumps(state.get('plan', {}), indent=2, default=str)}"
            )
            state = await _run_single_agent("muhtasib", state, muhtasib_message)

            # ── Detect new verdict via hash comparison ─────────────────
            current_verdict_hash = state.get("verdict_event_hash")
            new_verdict_committed = (
                current_verdict_hash is not None
                and current_verdict_hash != prev_verdict_hash
            )

            verdict = state.get("verdict", {})
            decision = verdict.get("decision", "") if new_verdict_committed else ""

            # ── Bounded single retry if first pass missed ──────────────
            if not new_verdict_committed:
                logger.warning(
                    "Muhtasib did not commit verdict for %s on first pass "
                    "(challenges_used=%d) — attempting bounded retry",
                    incident_id, challenges_used,
                )
                state["first_pass_commit"] = False
                state["retry_used"] = True

                retry_message = (
                    "IMPORTANT: You MUST call the commit_verdict tool NOW.\n"
                    "You have already reviewed the plan and produced reasoning. "
                    "Your ONLY task is to call commit_verdict with your decision.\n"
                    "Do NOT produce any other output — call the tool immediately.\n\n"
                    f"{muhtasib_message}"
                )
                state = await _run_single_agent(
                    "muhtasib", state, retry_message,
                )

                # Check again — compare against the SAME prev_verdict_hash
                retry_verdict_hash = state.get("verdict_event_hash")
                retry_committed = (
                    retry_verdict_hash is not None
                    and retry_verdict_hash != prev_verdict_hash
                )

                if retry_committed:
                    verdict = state.get("verdict", {})
                    decision = verdict.get("decision", "")
                    logger.info(
                        "Muhtasib retry succeeded for %s (challenges_used=%d) — "
                        "decision=%s, first_pass_commit=False, retry_used=True",
                        incident_id, challenges_used, decision,
                    )
                else:
                    # ── BOTH ATTEMPTS FAILED — Forced ESCALATE ─────────
                    logger.warning(
                        "Muhtasib retry also failed for %s (challenges_used=%d) — "
                        "escalating (no auto-approve without safety review)",
                        incident_id, challenges_used,
                    )

                    verdict = {
                        "decision": "ESCALATE",
                        "risk_score": 0.9,
                        "reasoning": (
                            "Forced verdict finalizer: safety reviewer "
                            "did not commit verdict after bounded retry — "
                            "escalating for manual SRE review (no auto-approve)"
                        ),
                        "challenge_target": None,
                        "challenge": None,
                        "policy_findings": [
                            "Forced finalizer — no safety review was committed "
                            "after two attempts. "
                            "Escalating per policy: missing review = no execution."
                        ],
                        "first_pass_commit": False,
                        "retry_used": True,
                    }

                    # Persist the synthetic verdict event — atomic transition
                    # decision value is "ESCALATE", incident status is "ESCALATED"
                    event, _ = await store.append_event_and_room_message(
                        incident_id=incident_id,
                        run_id=run_id,
                        actor="muhtasib",
                        actor_role="safety_reviewer",
                        event_type="safety_review_completed",
                        summary=(
                            "Safety verdict: ESCALATE "
                            "(forced verdict finalizer — bounded retry exhausted)"
                        ),
                        payload=verdict,
                        room_sender="muhtasib",
                        room_content=(
                            "⚖️ Safety review INCOMPLETE — escalating to human SRE. "
                            "No plan will be executed without explicit safety approval."
                        ),
                        room_mentions=None,
                        room_message_type="verdict",
                        transition_from="REVIEWING",
                        transition_to="ESCALATED",
                    )

                    state["verdict"] = verdict
                    state["verdict_event_hash"] = event["event_hash"]
                    decision = "ESCALATE"
                    logger.info(
                        "Forced verdict finalizer: escalated %s "
                        "with decision=ESCALATE (event_hash=%s)",
                        incident_id,
                        event["event_hash"],
                    )

            # ── Route based on decision ────────────────────────────────
            if decision == "APPROVED_REQUIRES_HUMAN":
                break  # proceed to contract issuance
            elif decision == "BLOCKED_UNSAFE":
                # Muhtasib's commit_verdict already transitioned to BLOCKED
                await store.complete_pipeline_run(run_id)
                logger.info("Phase 1 blocked: unsafe plan for %s", incident_id)
                return
            elif decision == "ESCALATE":
                # Muhtasib's commit_verdict already transitioned to ESCALATED
                await store.complete_pipeline_run(run_id)
                logger.info("Phase 1 escalated: %s", incident_id)
                return
            elif decision == "CHALLENGE":
                # ── Check challenge budget BEFORE producing a revision ─
                if challenges_used >= MAX_CHALLENGE_ROUNDS:
                    logger.warning(
                        "Challenge limit reached for %s "
                        "(challenges_used=%d, MAX=%d) — escalating "
                        "WITHOUT generating another revision",
                        incident_id, challenges_used, MAX_CHALLENGE_ROUNDS,
                    )
                    # Muhtasib's commit_verdict may have already
                    # transitioned the incident out of REVIEWING,
                    # so use a direct status update (not CAS).
                    await store.update_incident(
                        incident_id, status="ESCALATED",
                    )
                    await store.append_event(
                        incident_id=incident_id, run_id=run_id,
                        actor="gateway", actor_role="system",
                        event_type="challenge_limit_reached",
                        summary=(
                            f"Max challenge rounds ({MAX_CHALLENGE_ROUNDS}) "
                            f"exceeded — escalating without further revision"
                        ),
                        payload={
                            "challenges_used": challenges_used,
                            "max_rounds": MAX_CHALLENGE_ROUNDS,
                        },
                    )
                    await store.complete_pipeline_run(run_id)
                    return

                # ── Budget available — perform the revision ────────────
                challenges_used += 1
                target = verdict.get("challenge_target", "")

                # Accumulate ALL challenge feedback so Mudabbir never
                # loses memory of prior rejections (e.g. after EVIDENCE
                # challenge wipes context of an earlier PLAN challenge).
                round_feedback = (
                    f"[Round {challenges_used} — {target} challenge] "
                    f"{verdict.get('reasoning', '')}"
                )
                all_challenge_feedback.append(round_feedback)
                accumulated_feedback = "\n\n".join(all_challenge_feedback)
                state["challenge_feedback"] = accumulated_feedback

                if target == "EVIDENCE":
                    # Re-investigate with challenge context
                    prev_inv_hash = state.get("investigation_event_hash")
                    reinvestigate_msg = (
                        f"{alert_text}\n\n"
                        f"=== SAFETY CHALLENGE: EVIDENCE INSUFFICIENT ===\n"
                        f"{verdict.get('reasoning', '')}\n\n"
                        f"=== PREVIOUS INVESTIGATION ===\n"
                        f"{json.dumps(state.get('investigation_result', {}), indent=2, default=str)}"
                    )
                    state = await _run_single_agent(
                        "muhaqqiq", state, reinvestigate_msg,
                        thinking_level="HIGH",
                    )
                    if state.get("investigation_event_hash") == prev_inv_hash:
                        await store.fail_pipeline_once(
                            incident_id=incident_id,
                            run_id=run_id,
                            phase="phase1",
                            error_type="MissingInvestigationRevisionCommit",
                            error_message="Muhaqqiq did not commit a revised investigation after evidence challenge.",
                        )
                        return
                    investigation = state.get("investigation_result", investigation)
                    # Re-plan with updated investigation
                    prev_plan_hash = state.get("plan_event_hash")
                    replan_msg = (
                        f"{alert_text}\n\n"
                        f"=== TRIAGE RESULT ===\n"
                        f"{json.dumps(triage, indent=2, default=str)}\n\n"
                        f"=== UPDATED INVESTIGATION RESULT ===\n"
                        f"{json.dumps(investigation, indent=2, default=str)}\n\n"
                        f"=== ALL SAFETY CHALLENGE FEEDBACK (DO NOT repeat rejected actions) ===\n"
                        f"{accumulated_feedback}"
                    )
                    state = await _run_single_agent(
                        "mudabbir", state, replan_msg,
                        thinking_level="HIGH",
                    )
                    if state.get("plan_event_hash") == prev_plan_hash:
                        await store.fail_pipeline_once(
                            incident_id=incident_id,
                            run_id=run_id,
                            phase="phase1",
                            error_type="MissingPlanRevisionCommit",
                            error_message="Mudabbir did not commit a revised plan after evidence challenge.",
                        )
                        return
                elif target == "PLAN":
                    state["plan_revision"] = state.get("plan_revision", 1) + 1
                    prev_plan_hash = state.get("plan_event_hash")
                    revision_msg = (
                        f"{alert_text}\n\n"
                        f"=== TRIAGE RESULT ===\n"
                        f"{json.dumps(triage, indent=2, default=str)}\n\n"
                        f"=== INVESTIGATION RESULT ===\n"
                        f"{json.dumps(investigation, indent=2, default=str)}\n\n"
                        f"=== ALL SAFETY CHALLENGE FEEDBACK (DO NOT repeat rejected actions) ===\n"
                        f"{accumulated_feedback}\n\n"
                        f"=== PREVIOUS PLAN ===\n"
                        f"{json.dumps(state.get('plan', {}), indent=2, default=str)}"
                    )
                    state = await _run_single_agent(
                        "mudabbir", state, revision_msg,
                        thinking_level="HIGH",
                    )
                    if state.get("plan_event_hash") == prev_plan_hash:
                        await store.fail_pipeline_once(
                            incident_id=incident_id,
                            run_id=run_id,
                            phase="phase1",
                            error_type="MissingPlanRevisionCommit",
                            error_message="Mudabbir did not commit a revised plan after plan challenge.",
                        )
                        return
                # loop back to muhtasib for review of the new revision
            else:
                # Unknown verdict — treat as escalation
                # Muhtasib's commit_verdict may have already
                # transitioned the incident, so use direct update.
                await store.update_incident(
                    incident_id, status="ESCALATED",
                )
                await store.complete_pipeline_run(run_id)
                return

        # ── Step 5: Issue approval contract ──────────────────────────────
        plan = state.get("plan", {})
        plan_json = canonical_json(plan)
        plan_hash = sha256_hex(plan)
        plan_event_hash = state.get("plan_event_hash", "")
        actions_json = canonical_json(plan.get("actions", []))
        nonce = generate_approval_nonce()

        # Build token claims and compute digest
        claims = build_token_claims(
            incident_id=incident_id,
            contract_id="pending",
            revision=plan.get("revision", 1),
            plan_hash=plan_hash,
            nonce=nonce,
            ttl_seconds=settings.token_ttl,
        )
        token = token_mgr.generate_token(claims)
        digest = ApprovalTokenManager.token_digest(token)

        expires_at = datetime.fromtimestamp(
            claims["exp"], tz=timezone.utc
        ).isoformat()

        # Issue contract first with placeholder claims
        contract = await store.issue_contract(
            incident_id=incident_id,
            revision=plan.get("revision", 1),
            plan_id=plan.get("plan_id", "UNKNOWN"),
            plan_hash=plan_hash,
            plan_event_hash=plan_event_hash,
            canonical_plan_json=plan_json,
            actions_json=actions_json,
            approval_nonce=nonce,
            token_digest=digest,
            expires_at=expires_at,
            claims_json="",  # placeholder, updated below
        )

        # Rebuild claims with actual contract_id and store final claims
        claims["contract_id"] = contract["contract_id"]
        claims_json = json.dumps(claims, sort_keys=True)
        token = token_mgr.generate_token(claims)
        digest = ApprovalTokenManager.token_digest(token)

        await store.transition_contract(
            incident_id=incident_id,
            revision=plan.get("revision", 1),
            from_status="ISSUED",
            to_status="ISSUED",
            token_digest=digest,
            claims_json=claims_json,
        )

        # Append contract_issued event with room message
        await store.append_event_and_room_message(
            incident_id=incident_id, run_id=run_id,
            event_type="contract_issued",
            actor="gateway", actor_role="system",
            summary=f"Approval contract issued: {contract['contract_id']}",
            payload={
                "contract_id": contract["contract_id"],
                "revision": plan.get("revision", 1),
                "plan_id": plan.get("plan_id", "UNKNOWN"),
                "plan_hash": plan_hash,
                "expires_at": expires_at,
            },
            room_sender="system",
            room_content=f"\u2501\u2501\u2501 \U0001F510 HUMAN APPROVAL BOUNDARY \u2501\u2501\u2501\nContract {contract['contract_id']} issued. Awaiting human authorization.\nPlan hash: {plan_hash[:16]}... | Expires: {expires_at}",
            room_message_type="system",
        )

        await store.transition_incident(
            incident_id=incident_id,
            from_status=IncidentStatus.REVIEWING.value,
            to_status=IncidentStatus.AWAITING_APPROVAL.value,
        )

        await store.complete_pipeline_run(run_id)
        logger.info(
            "Contract issued: %s for incident %s",
            contract["contract_id"], incident_id,
        )

    except Exception as exc:
        logger.exception("Phase 1 pipeline failed: %s", exc)
        await store.fail_pipeline_once(
            incident_id=incident_id,
            run_id=run_id,
            phase="phase1",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


# ═══════════════════════════════════════════════════════════════════════════
# 8. Phase 2 Execution (background task)
# ═══════════════════════════════════════════════════════════════════════════

async def _run_phase2_execution(
    incident_id: str,
    run_id: str,
    contract: dict,
) -> None:
    """
    Phase 2: Aamil executes, gateway verifies recovery.

    Single execution path through Aamil agent only.
    Gateway performs deterministic recovery verification post-execution.
    """
    store = _get_store()
    token_mgr = _get_token_manager()

    try:
        logger.info("Phase 2 starting: incident=%s", incident_id)

        # ── Reconstruct HMAC claims from the stored contract ─────────────
        # These were stored as claims_json at contract-issuance time.
        stored_claims_json = contract.get("claims_json", "{}")
        if not stored_claims_json:
            stored_claims_json = "{}"
        hmac_claims = json.loads(stored_claims_json)

        # ── Atomic revalidation + APPROVED → EXECUTING in one transaction ─
        # Checks: contract status, canonical_plan_json hash, claims.plan_hash,
        # actions_json consistency. Returns an immutable execution snapshot.
        ok, reason, snapshot = await store.claim_execution_snapshot(
            incident_id=incident_id,
            contract_id=contract["contract_id"],
            revision=contract["revision"],
            run_id=run_id,
            hmac_claims=hmac_claims,
            token_manager=token_mgr,
        )

        if not ok:
            # Pre-execution validation failed
            logger.error(
                "Phase 2 pre-execution validation FAILED for %s: %s",
                incident_id, reason,
            )
            # Only invalidate for actual tamper — not benign states like
            # contract_not_approved:EXECUTING (duplicate execution attempt)
            _TAMPER_REASONS = {
                "canonical_plan_hash_mismatch",
                "claims_plan_hash_mismatch",
                "actions_json_divergence",
            }
            if reason in _TAMPER_REASONS:
                await store.invalidate_tampered_contract(
                    incident_id=incident_id,
                    contract_id=contract["contract_id"],
                    run_id=run_id,
                    reason=reason,
                )
            return

        # --- Run Aamil via ADK Runner ---
        # No live objects in session state.
        # The execution_snapshot is the SOLE authority for what Aamil may
        # execute. It was atomically validated by claim_execution_snapshot()
        # and contains actions derived exclusively from canonical_plan_json.
        # Aamil reads snapshot["actions"] directly — no DB fetch, no fallback.
        state = {
            "incident_id": incident_id,
            "run_id": run_id,
            "execution_snapshot": snapshot,
        }
        state = await _run_single_agent(
            "aamil", state, "Execute the approved remediation plan.",
        )
        reconciliation = state.get("reconciliation", {})
        if not reconciliation:
            # Build reconciliation from execution receipts
            receipts = state.get("execution_receipts", {})
            all_ok = state.get("all_actions_succeeded", False)
            succeeded = sum(1 for r in receipts.values() if r.get("status") == "success")
            total = len(receipts)
            if all_ok:
                rec_status = "all_succeeded"
            elif succeeded > 0:
                rec_status = "partial"
            else:
                rec_status = "all_failed"
            reconciliation = {
                "status": rec_status,
                "receipts": receipts,
                "succeeded": succeeded,
                "total": total,
            }

        # --- Deterministic recovery verification (gateway, not agent) ---
        from shared.recovery_verifier import verify_recovery

        alert_json = (await store.get_incident(incident_id) or {}).get(
            "alert_json", "{}"
        )
        alert_data = json.loads(alert_json)
        service_id = alert_data.get("service_id", "unknown")

        # Only pass victim_url in sandbox mode — simulated mode must
        # never trigger real HTTP health checks against the victim.
        execution_mode = os.environ.get("MUHAFIZ_EXECUTION_MODE", "simulated").lower()
        victim_url = (
            os.environ.get("VICTIM_SERVICE_URL")
            if execution_mode == "sandbox"
            else None
        )

        recovery = await verify_recovery(
            service_id=service_id, victim_url=victim_url,
        )

        await store.append_event_and_room_message(
            incident_id=incident_id, run_id=run_id,
            event_type="recovery_verified",
            actor="gateway", actor_role="system",
            summary=f"Recovery: {recovery['status']} (score: {recovery['recovery_score']:.2f})",
            payload=recovery,
            room_sender="aamil",
            room_content=f"\u26a1 Recovery verification: {recovery['status']} (score: {recovery['recovery_score']:.0%})",
        )

        # --- Final status determination ---
        exec_status = reconciliation.get("status", "all_failed")
        if exec_status == "all_succeeded" and recovery["status"] == "RECOVERED":
            final_status = "RESOLVED"
        elif exec_status == "all_succeeded":
            final_status = "RECOVERY_FAILED"
        elif exec_status == "partial":
            final_status = "DEGRADED"
        else:
            final_status = "EXECUTION_FAILED"

        # Finalize with seal – use snapshot (immutable, revalidated source)
        result = await store.finalize_incident(
            incident_id=incident_id,
            run_id=run_id,
            contract_id=snapshot["contract_id"],
            revision=snapshot["revision"],
            final_status=final_status,
            reconciliation=reconciliation,
            recovery=recovery,
        )

        logger.info(
            "Incident finalized: %s → %s (seal: %s)",
            incident_id, final_status,
            result["final_event_hash"][:12],
        )

    except Exception as exc:
        logger.exception("Phase 2 execution failed: %s", exc)
        await store.fail_pipeline_once(
            incident_id=incident_id,
            run_id=run_id,
            phase="phase2",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


# ═══════════════════════════════════════════════════════════════════════════
# 9. Revision Pipeline (background task)
# ═══════════════════════════════════════════════════════════════════════════

async def _run_revision_pipeline(
    incident_id: str,
    feedback: str,
) -> None:
    """
    Re-run Mudabbir → Muhtasib with revision feedback.

    Only re-plans and re-reviews; does not re-triage or re-investigate.
    """
    store = _get_store()

    try:
        incident = await store.get_incident(incident_id)
        if incident is None:
            return

        current_revision = incident.get("active_revision", 0) + 1
        await store.update_incident(
            incident_id, active_revision=current_revision,
        )

        run = await store.claim_pipeline_run(
            incident_id=incident_id,
            phase="phase1",
            revision=current_revision,
            start_stage="planning",
            input_data={"revision_feedback": feedback, "revision": current_revision},
        )
        run_id = run["run_id"]

        # Build state with existing investigation + revision context
        alert_json = incident.get("alert_json", "{}")
        alert_data = json.loads(alert_json)

        state = {
            "incident_id": incident_id,
            "run_id": run_id,
            "alert": alert_data,
            "plan_revision": current_revision,
            "revision_feedback": feedback,
        }

        alert_text = (
            f"Revise the remediation plan (revision {current_revision}).\n"
            f"Feedback: {feedback}\n"
            f"Service: {alert_data.get('service_id', 'unknown')}\n"
            f"Summary: {alert_data.get('summary', '')}"
        )

        # Re-run only Mudabbir → Muhtasib (not full pipeline)
        state = await _run_single_agent("mudabbir", state, alert_text)
        state = await _run_single_agent("muhtasib", state, alert_text)

        verdict = state.get("verdict", {})
        decision = verdict.get("decision", "")

        if decision == "APPROVED_REQUIRES_HUMAN":
            # Re-issue contract with new revision
            settings = _get_settings()
            token_mgr = _get_token_manager()
            plan = state.get("plan", {})
            plan_json = canonical_json(plan)
            plan_hash = sha256_hex(plan)
            plan_event_hash = state.get("plan_event_hash", "")
            actions_json = canonical_json(plan.get("actions", []))
            nonce = generate_approval_nonce()

            claims = build_token_claims(
                incident_id=incident_id,
                contract_id="pending",
                revision=current_revision,
                plan_hash=plan_hash,
                nonce=nonce,
                ttl_seconds=settings.token_ttl,
            )
            token = token_mgr.generate_token(claims)
            digest = ApprovalTokenManager.token_digest(token)
            expires_at = datetime.fromtimestamp(
                claims["exp"], tz=timezone.utc,
            ).isoformat()
            # Issue contract first with placeholder claims
            contract = await store.issue_contract(
                incident_id=incident_id,
                revision=current_revision,
                plan_id=plan.get("plan_id", "UNKNOWN"),
                plan_hash=plan_hash,
                plan_event_hash=plan_event_hash,
                canonical_plan_json=plan_json,
                actions_json=actions_json,
                approval_nonce=nonce,
                token_digest=digest,
                expires_at=expires_at,
                claims_json="",  # placeholder, updated below
            )

            # Rebuild claims with actual contract_id and store final claims
            claims["contract_id"] = contract["contract_id"]
            claims_json = json.dumps(claims, sort_keys=True)
            token = token_mgr.generate_token(claims)
            digest = ApprovalTokenManager.token_digest(token)
            await store.transition_contract(
                incident_id=incident_id,
                revision=current_revision,
                from_status="ISSUED", to_status="ISSUED",
                token_digest=digest,
                claims_json=claims_json,
            )

            await store.append_event_and_room_message(
                incident_id=incident_id, run_id=run_id,
                event_type="contract_issued",
                actor="gateway", actor_role="system",
                summary=f"Revised contract issued: {contract['contract_id']} (rev {current_revision})",
                payload={
                    "contract_id": contract["contract_id"],
                    "revision": current_revision,
                    "plan_hash": plan_hash,
                    "expires_at": expires_at,
                },
                room_sender="system",
                room_content=f"\u2501\u2501\u2501 \U0001F510 REVISION {current_revision} APPROVAL BOUNDARY \u2501\u2501\u2501\nRevised contract {contract['contract_id']} issued. Awaiting human authorization.",
                room_message_type="system",
            )

            await store.transition_incident(
                incident_id=incident_id,
                from_status="REVISION_REQUESTED",
                to_status="AWAITING_APPROVAL",
            )

        await store.complete_pipeline_run(run_id)

    except Exception as exc:
        logger.exception("Revision pipeline failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# 10. Legacy compatibility endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/ledger/verify", tags=["Audit"])
async def verify_legacy_chain():
    """Legacy endpoint for chain verification (backward compatibility)."""
    return {
        "chain_valid": True,
        "message": "Use /api/incidents/{id}/chain/verify for per-incident verification.",
        "verified_at": _utc_now(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MUHAFIZ_HOST", "0.0.0.0")
    port = int(os.environ.get("MUHAFIZ_PORT", "8000"))
    reload_flag = os.environ.get("MUHAFIZ_RELOAD", "true").lower() == "true"

    uvicorn.run(
        "gateway.app:app",
        host=host,
        port=port,
        reload=reload_flag,
        log_level="info",
    )
