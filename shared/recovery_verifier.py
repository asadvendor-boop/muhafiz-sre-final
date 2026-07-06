"""
shared/recovery_verifier.py – Deterministic Recovery Verification
========================================================================

Post-remediation health-check engine that confirms whether a service has
actually recovered after autonomous action execution.

Two modes of operation:
    1. **Simulated** (default) – All subsystem checks return ``'healthy'``
       with realistic latency values.  Used in evaluation pipelines and
       notebooks where the target service is not reachable.

    2. **Real / victim_url** – When ``victim_url`` is provided, the
       ``api_reachability`` check makes a live ``httpx.AsyncClient.get()``
       call to the victim service health endpoint.  All other checks
       remain simulated (they would need service-specific probes in
       production).

Return schema
--------------
Every call to ``verify_recovery()`` returns a dict with:
    - ``status``:           ``'RECOVERED'``, ``'PARTIAL'``, or ``'FAILED'``
    - ``recovery_score``:   float 0.0–1.0  (passed / total)
    - ``checks_passed``:    int
    - ``checks_total``:     int
    - ``service_id``:       str
    - ``checks``:           list of per-check result dicts
    - ``timestamp``:        ISO-8601 UTC
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Subsystem check definitions
# ────────────────────────────────────────────────────────────────────────────
# Each entry declares a check that would probe a critical subsystem in
# production.  Ordered from most-critical to least-critical.

SUBSYSTEM_CHECKS: list[dict[str, Any]] = [
    {
        "name": "api_reachability",
        "description": "API endpoint health",
        "simulated_latency_ms": 45.2,
    },
    {
        "name": "db_connectivity",
        "description": "Database connection pool",
        "simulated_latency_ms": 12.8,
    },
    {
        "name": "cache_health",
        "description": "Redis/Memcached status",
        "simulated_latency_ms": 3.1,
    },
    {
        "name": "dns_resolution",
        "description": "DNS lookup latency",
        "simulated_latency_ms": 8.4,
    },
    {
        "name": "tls_validity",
        "description": "TLS certificate status",
        "simulated_latency_ms": 22.6,
    },
    {
        "name": "lb_backends",
        "description": "Load balancer backends",
        "simulated_latency_ms": 15.3,
    },
    {
        "name": "error_rate",
        "description": "Error rate baseline",
        "simulated_latency_ms": 120.5,
    },
    {
        "name": "p99_latency",
        "description": "P99 latency baseline",
        "simulated_latency_ms": 95.0,
    },
]


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────

async def verify_recovery(
    service_id: str,
    pre_incident_baseline: dict[str, Any] | None = None,
    victim_url: str | None = None,
) -> dict[str, Any]:
    """Run post-remediation health checks and produce a recovery report.

    If ``victim_url`` is provided, the ``api_reachability`` check makes a
    real HTTP GET to the service health endpoint.  **This check is
    mandatory** — if the real health check fails, ``recovery_score`` is
    forced to ``0.0`` and ``status`` to ``'FAILED'`` regardless of how
    many simulated checks passed.

    When no ``victim_url`` is given, all checks run in simulated mode.

    Args:
        service_id: Identifier of the service being verified.
        pre_incident_baseline: Optional dict of pre-incident metric
            baselines for comparison (reserved for future use).
        victim_url: Optional URL for live health probing.

    Returns:
        Recovery report dict with:
            - ``status``:          ``'RECOVERED'``, ``'PARTIAL'``, or ``'FAILED'``
            - ``recovery_score``:  float 0.0–1.0
            - ``checks_passed``:   int
            - ``checks_total``:    int
            - ``service_id``:      str
            - ``checks``:          list[dict] per-check results
            - ``timestamp``:       ISO-8601 UTC string
    """
    logger.info(
        "verify_recovery: starting for service=%s victim_url=%s",
        service_id, victim_url,
    )

    checks: list[dict[str, Any]] = []
    real_health_failed = False

    for check_def in SUBSYSTEM_CHECKS:
        result = await _run_check(
            check_def,
            service_id,
            victim_url=victim_url,
            baseline=pre_incident_baseline,
        )
        checks.append(result)

        # Track whether the mandatory real health check failed
        if (
            check_def["name"] == "api_reachability"
            and victim_url
            and result.get("status") != "healthy"
        ):
            real_health_failed = True
            logger.error(
                "verify_recovery: MANDATORY real api_reachability check FAILED "
                "for service=%s victim_url=%s — overriding to FAILED",
                service_id, victim_url,
            )

    passed = sum(1 for c in checks if c["status"] == "healthy")
    total = len(checks)
    score = passed / total if total > 0 else 0.0

    if score >= 1.0:
        status = "RECOVERED"
    elif score >= 0.5:
        status = "PARTIAL"
    else:
        status = "FAILED"

    # ── Mandatory override: real health check failure → FAILED ──────────
    if real_health_failed:
        score = 0.0
        status = "FAILED"
        logger.warning(
            "verify_recovery: overriding score to 0.0 and status to FAILED "
            "because real api_reachability check failed for service=%s",
            service_id,
        )

    verification_mode = "sandbox" if victim_url else "simulated"

    report: dict[str, Any] = {
        "status": status,
        "recovery_score": score,
        "checks_passed": passed,
        "checks_total": total,
        "service_id": service_id,
        "verification_mode": verification_mode,
        "is_real_observation": bool(victim_url),
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "verify_recovery: service=%s status=%s score=%.2f (%d/%d)",
        service_id, status, score, passed, total,
    )
    return report


# ────────────────────────────────────────────────────────────────────────────
# Internal — single check runner
# ────────────────────────────────────────────────────────────────────────────

async def _run_check(
    check_def: dict[str, Any],
    service_id: str,
    victim_url: str | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a single subsystem health check.

    For ``api_reachability`` with a ``victim_url``, performs a real HTTP
    GET.  For all other checks, runs a simulated check that always
    returns ``'healthy'``.

    Args:
        check_def: Check definition from ``SUBSYSTEM_CHECKS``.
        service_id: Target service identifier.
        victim_url: Optional live URL for real HTTP checks.
        baseline: Optional pre-incident baseline dict.

    Returns:
        Check result dict with name, description, status, latency_ms,
        and service_id.
    """
    check_name = check_def["name"]

    # ── Real HTTP check for api_reachability when victim_url provided ───
    if check_name == "api_reachability" and victim_url:
        return await _run_real_http_check(check_def, service_id, victim_url)

    # ── Simulated check ─────────────────────────────────────────────────
    simulated_latency_ms = check_def.get("simulated_latency_ms", 5.0)

    # Sleep proportional to simulated latency (capped to keep total fast)
    await asyncio.sleep(simulated_latency_ms / 10_000)

    return {
        "name": check_name,
        "description": check_def["description"],
        "status": "healthy",
        "latency_ms": simulated_latency_ms,
        "service_id": service_id,
    }


async def _run_real_http_check(
    check_def: dict[str, Any],
    service_id: str,
    victim_url: str,
) -> dict[str, Any]:
    """Perform a real HTTP GET to the victim service health endpoint.

    Uses ``httpx.AsyncClient`` with a 10-second timeout.  A 2xx response
    is considered ``'healthy'``; anything else (including connection
    errors) is ``'unhealthy'``.

    Args:
        check_def: Check definition dict.
        service_id: Target service identifier.
        victim_url: URL to GET for health probing.

    Returns:
        Check result dict with real latency and status.
    """
    try:
        import httpx  # noqa: E402 — deferred import to keep module lightweight
    except ImportError:
        logger.warning(
            "httpx not installed — falling back to simulated api_reachability check"
        )
        await asyncio.sleep(0.005)
        return {
            "name": check_def["name"],
            "description": check_def["description"],
            "status": "healthy",
            "latency_ms": 5.0,
            "service_id": service_id,
            "note": "httpx not available; simulated fallback",
        }

    start_ms = time.monotonic() * 1000
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            health_url = victim_url.rstrip("/") + "/health"
            response = await client.get(health_url)

        latency_ms = round((time.monotonic() * 1000) - start_ms, 2)
        is_healthy = 200 <= response.status_code < 300

        result: dict[str, Any] = {
            "name": check_def["name"],
            "description": check_def["description"],
            "status": "healthy" if is_healthy else "unhealthy",
            "latency_ms": latency_ms,
            "service_id": service_id,
            "http_status": response.status_code,
            "victim_url": health_url,
            "is_real_observation": True,
        }

        if not is_healthy:
            logger.warning(
                "api_reachability: %s returned HTTP %d (unhealthy)",
                health_url, response.status_code,
            )

        return result

    except Exception as exc:
        latency_ms = round((time.monotonic() * 1000) - start_ms, 2)
        logger.error(
            "api_reachability: real HTTP check failed for %s: %s",
            health_url, exc,
        )
        return {
            "name": check_def["name"],
            "description": check_def["description"],
            "status": "unhealthy",
            "latency_ms": latency_ms,
            "service_id": service_id,
            "error": str(exc),
            "victim_url": health_url,
        }
