"""
evaluation/victim/app.py – Simulated Victim Service for Testing
================================================================

A lightweight FastAPI application that simulates the ``auth-service``
microservice used in MuhafizSRE evaluation scenarios.  It exposes
endpoints that the evaluation harness uses to:

1. **Inject faults** — simulate error-rate spikes and latency
   degradation so the AI agent can detect and remediate them.
2. **Recover** — reset the service to a healthy baseline so
   post-remediation health checks pass.
3. **Validate tokens** — mimic a realistic JWT-validation endpoint
   whose behaviour degrades under fault conditions.

This service is designed to run inside Docker Compose alongside the
MuhafizSRE gateway and is referenced by the ``VICTIM_SERVICE_URL``
environment variable.

Running locally::

    uvicorn evaluation.victim.app:app --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ═══════════════════════════════════════════════════════════════════════════════
# Application Instance
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="MuhafizSRE Victim Service (auth-service)",
    description=(
        "Simulated auth-service microservice for MuhafizSRE sandbox testing. "
        "Supports programmable fault injection and recovery to evaluate "
        "the AI agent's incident remediation capabilities."
    ),
    version="5.0.0",
)

# ═══════════════════════════════════════════════════════════════════════════════
# Mutable Service State
# ═══════════════════════════════════════════════════════════════════════════════
# In-memory state dict — intentionally simple.  Each fault-injection
# call mutates this, and ``/recover`` resets it.  No persistence needed
# because the service lifetime matches the Docker container.

_state: dict[str, Any] = {
    "healthy": True,
    "error_rate": 0.0,
    "latency_ms": 50,
    "version": "v5.2.9",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Health & Observability
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/health", tags=["observability"])
async def health() -> JSONResponse:
    """Return current service health status.

    Returns HTTP **200** when the service is healthy and HTTP **503**
    (Service Unavailable) when in a faulted state.  This allows upstream
    health checkers to rely on status codes rather than parsing JSON.

    Returns:
        JSON with ``status``, ``version``, and ``latency_ms`` (healthy)
        or ``status`` and ``error_rate`` (unhealthy, HTTP 503).
    """
    if not _state["healthy"]:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error_rate": _state["error_rate"]},
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "version": _state["version"],
            "latency_ms": _state["latency_ms"],
        },
    )


@app.get("/status", tags=["observability"])
async def status() -> dict[str, Any]:
    """Return the full internal state of the service.

    Exposes all mutable state fields for debugging and evaluation,
    including ``healthy``, ``error_rate``, ``latency_ms``, and
    ``version``.

    Returns:
        JSON dict with every field from the internal ``_state``.
    """
    return {
        "healthy": _state["healthy"],
        "error_rate": _state["error_rate"],
        "latency_ms": _state["latency_ms"],
        "version": _state["version"],
        "injected_fault": not _state["healthy"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Fault Injection & Recovery
# ═══════════════════════════════════════════════════════════════════════════════


@app.post("/inject-fault", tags=["chaos"])
async def inject_fault(
    error_rate: float = 0.45,
    latency_ms: int = 5000,
) -> dict[str, Any]:
    """Inject a simulated fault into the service.

    Puts the service into an unhealthy state with configurable error
    rate and latency.  This endpoint is called by the evaluation runner
    *before* handing the incident to the AI agent.

    Args:
        error_rate: Probability (0.0–1.0) that ``/api/auth/validate``
            returns a failure response.  Defaults to 0.45 (45%).
        latency_ms: Artificial latency in milliseconds added to every
            ``/api/auth/validate`` call.  Defaults to 5000 (5 s).

    Returns:
        JSON confirming the fault was injected along with the updated
        internal state.
    """
    _state["healthy"] = False
    _state["error_rate"] = error_rate
    _state["latency_ms"] = latency_ms
    return {"status": "fault_injected", **_state}


@app.post("/recover", tags=["chaos"])
async def recover() -> dict[str, Any]:
    """Reset the service to a healthy baseline.

    Called by the evaluation harness (or recovery verifier) after the
    AI agent's remediation action to confirm that the service can
    return to normal operation.

    Returns:
        JSON confirming recovery along with the reset state values.
    """
    _state["healthy"] = True
    _state["error_rate"] = 0.0
    _state["latency_ms"] = 50
    return {"status": "recovered", **_state}


# ═══════════════════════════════════════════════════════════════════════════════
# Simulated Business Endpoint
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/auth/validate", tags=["auth"])
async def validate_token() -> dict[str, Any]:
    """Simulate JWT token validation.

    In healthy state, returns a successful validation after a small
    realistic delay.  When faults are injected, the response is
    delayed by ``latency_ms`` and fails with probability ``error_rate``.

    This endpoint is probed by the MuhafizSRE recovery verifier to
    determine whether the service has recovered after remediation.

    Returns:
        JSON with ``valid: True`` and ``user_id`` on success, or
        ``valid: False`` and ``error`` on simulated failure.
    """
    # Simulate network/processing latency
    await asyncio.sleep(_state["latency_ms"] / 1000)

    # Under fault conditions, randomly fail requests
    if not _state["healthy"] and random.random() < _state["error_rate"]:
        return {"valid": False, "error": "JWT validation failed"}

    return {"valid": True, "user_id": "test-user"}
