"""
shared/skills.py – Async Skill Adapters for MuhafizSRE
=============================================================

Every autonomous remediation action is packaged as an async skill function.
Each skill follows the contract:

    1. **Async** – all IO uses ``await`` / ``asyncio.sleep``.
    2. **Typed parameters** with runtime validation.
    3. **SkillResult-compatible return** – ``dict`` matching the
       ``gateway.models.SkillResult`` schema (status, execution_id,
       timestamp, service, detail, adapter).
    4. **Idempotency** – simulated mode returns structured results
       without side-effects; safe for dry-run pipelines.
    5. **Observability** – every invocation is logged start/complete.

Skills Catalogue (§16)
-----------------------------
| Skill                       | Remediation Target                  |
|-----------------------------|-------------------------------------|
| rollback_service_revision   | Cloud Run traffic rollback          |
| apply_rate_limit            | API gateway rate-limiting           |
| scale_service               | Cloud Run autoscaler tuning         |
| flush_cache                 | Redis cache invalidation            |
| rotate_credentials          | Secret Manager key rotation         |
| restart_service             | Kubernetes rollout restart          |

Execution Layer
----------------
| Function              | Purpose                              |
|-----------------------|--------------------------------------|
| execute_skill         | Registry lookup + single dispatch    |
| execute_action_graph  | Topological multi-action executor    |
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from gateway.models import AllowedSkill, FailurePolicy
from shared.action_policy import check_action_eligibility, topological_sort

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Helper Utilities
# ────────────────────────────────────────────────────────────────────────────

def _execution_id() -> str:
    """Generate a short, unique execution ID for tracing skill invocations.

    Uses the first 8 hex characters of a UUID-4 — short enough for log
    readability, unique enough for incident correlation.

    Returns:
        str: An 8-character hex string, e.g. ``'a3f1bc04'``.
    """
    return uuid.uuid4().hex[:8]


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format.

    All skill results embed this timestamp so the orchestrator can
    reconstruct a precise remediation timeline.

    Returns:
        str: UTC timestamp like ``'2026-06-20T12:23:56+00:00'``.
    """
    return datetime.now(timezone.utc).isoformat()


def _error_result(
    exec_id: str,
    service_name: str,
    error_msg: str,
) -> dict[str, Any]:
    """Build a standardised error result dict.

    Args:
        exec_id: Execution trace identifier.
        service_name: Target service that was being acted on.
        error_msg: Human-readable error description.

    Returns:
        dict matching SkillResult schema with ``status='error'``.
    """
    return {
        "status": "error",
        "execution_id": exec_id,
        "timestamp": _now_iso(),
        "service": service_name,
        "adapter": "simulated",
        "detail": {"error": error_msg},
    }


# ════════════════════════════════════════════════════════════════════════════
# Skill 1 — Cloud Run Revision Rollback
# ════════════════════════════════════════════════════════════════════════════

async def rollback_service_revision(
    service_name: str,
    target_revision: str,
    victim_url: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Roll back a Cloud Run service to a previous revision.

    Execution modes (controlled by ``MUHAFIZ_EXECUTION_MODE``):

    - **simulated** (default): Deterministic receipt with ``asyncio.sleep``.
      Safe for evaluation pipelines and benchmarks.
    - **sandbox**: Real HTTP POST to ``{victim_url}/recover`` with
      before/after state capture.  Fail-closed on any error.

    The ``is_real_mutation`` field in the receipt is the single source of
    truth for the dashboard's REAL SANDBOX MUTATION badge.  It is set
    ONLY when a real HTTP POST succeeds.

    Args:
        service_name: Cloud Run service identifier.
        target_revision: Revision tag to restore traffic to.
        victim_url: Optional URL of the victim service.  Defaults to
            ``os.environ.get("VICTIM_SERVICE_URL")``.
        **kwargs: Absorbed for forward-compatibility.

    Returns:
        SkillResult-compatible dict with rollback details.
    """
    exec_id = _execution_id()
    execution_mode = os.environ.get("MUHAFIZ_EXECUTION_MODE", "simulated")
    victim_url = victim_url or os.environ.get("VICTIM_SERVICE_URL")
    sandbox_target = os.environ.get("MUHAFIZ_SANDBOX_TARGET_SERVICE", "auth-service")

    logger.info(
        "[%s] SKILL START — rollback_service_revision | service=%s revision=%s "
        "mode=%s victim_url=%s",
        exec_id, service_name, target_revision, execution_mode, victim_url,
    )

    # ── Input validation ────────────────────────────────────────────────
    if not service_name or not service_name.strip():
        logger.error("[%s] service_name is empty", exec_id)
        return _error_result(exec_id, "", "service_name must be a non-empty string.")

    if not target_revision or not target_revision.strip():
        logger.error("[%s] target_revision is empty", exec_id)
        return _error_result(exec_id, service_name, "target_revision must be a non-empty string.")

    # ═══════════════════════════════════════════════════════════════════
    # SANDBOX MODE — fail closed, real HTTP mutation
    # ═══════════════════════════════════════════════════════════════════
    if execution_mode == "sandbox":
        # ── Guard: sandbox only supports the designated target ───────
        if service_name != sandbox_target:
            logger.error(
                "[%s] SANDBOX FAIL CLOSED — target '%s' is not the sandbox "
                "target '%s'. No fallback to simulation.",
                exec_id, service_name, sandbox_target,
            )
            return {
                "status": "error",
                "execution_id": exec_id,
                "timestamp": _now_iso(),
                "service": service_name,
                "adapter": "sandbox",
                "is_real_mutation": False,
                "detail": {
                    "error": (
                        f"Sandbox mode only supports '{sandbox_target}' mutations. "
                        f"Target '{service_name}' is not permitted."
                    ),
                    "execution_mode": "sandbox",
                },
            }

        # ── Guard: victim URL must be configured ────────────────────
        if not victim_url:
            logger.error(
                "[%s] SANDBOX FAIL CLOSED — VICTIM_SERVICE_URL not set.",
                exec_id,
            )
            return {
                "status": "error",
                "execution_id": exec_id,
                "timestamp": _now_iso(),
                "service": service_name,
                "adapter": "sandbox",
                "is_real_mutation": False,
                "detail": {
                    "error": (
                        "Sandbox mode requires VICTIM_SERVICE_URL to be set. "
                        "Cannot fall back to simulation."
                    ),
                    "execution_mode": "sandbox",
                },
            }

        # ── Real sandbox execution ──────────────────────────────────
        try:
            import httpx  # noqa: E402 — deferred import
        except ImportError:
            logger.error(
                "[%s] SANDBOX FAIL CLOSED — httpx not installed.",
                exec_id,
            )
            return {
                "status": "error",
                "execution_id": exec_id,
                "timestamp": _now_iso(),
                "service": service_name,
                "adapter": "sandbox",
                "is_real_mutation": False,
                "detail": {
                    "error": "httpx not installed. Cannot execute sandbox mutation.",
                    "execution_mode": "sandbox",
                },
            }

        status_url = f"{victim_url.rstrip('/')}/status"
        recover_url = f"{victim_url.rstrip('/')}/recover"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Step 1: Capture BEFORE state
                before_resp = await client.get(status_url)
                before_state = before_resp.json()

                # Step 2: Execute real recovery mutation
                logger.info(
                    "[%s] SANDBOX — POSTing to %s", exec_id, recover_url,
                )
                recover_resp = await client.post(recover_url)
                recover_data = recover_resp.json()
                recovered = recover_data.get("status") == "recovered"

                # Step 3: Capture AFTER state
                after_resp = await client.get(status_url)
                after_state = after_resp.json()

        except httpx.TimeoutException:
            logger.error(
                "[%s] SANDBOX FAIL CLOSED — timeout connecting to %s",
                exec_id, victim_url,
            )
            return {
                "status": "error",
                "execution_id": exec_id,
                "timestamp": _now_iso(),
                "service": service_name,
                "adapter": "sandbox",
                "is_real_mutation": False,
                "detail": {
                    "error": f"Timeout connecting to victim service at {victim_url}",
                    "execution_mode": "sandbox",
                },
            }
        except Exception as exc:
            logger.error(
                "[%s] SANDBOX FAIL CLOSED — %s: %s",
                exec_id, type(exc).__name__, exc,
            )
            return {
                "status": "error",
                "execution_id": exec_id,
                "timestamp": _now_iso(),
                "service": service_name,
                "adapter": "sandbox",
                "is_real_mutation": False,
                "detail": {
                    "error": f"Sandbox execution failed: {exc}",
                    "execution_mode": "sandbox",
                },
            }

        # ── Build provenance-rich receipt ────────────────────────────
        if not recovered:
            logger.error(
                "[%s] SANDBOX — victim did not return 'recovered': %s",
                exec_id, recover_data,
            )

        result: dict[str, Any] = {
            "status": "success" if recovered else "error",
            "execution_id": exec_id,
            "timestamp": _now_iso(),
            "service": service_name,
            "adapter": "sandbox",
            "is_real_mutation": recovered,
            "detail": {
                "operation": "rollback",
                "target_revision": target_revision,
                "execution_mode": "sandbox",
                "endpoint": recover_url,
                "http_status": recover_resp.status_code,
                "victim_response": recover_data,
                "before_state": before_state,
                "after_state": after_state,
                "recovery_verified": recovered,
                "rollback_completed": recovered,
            },
        }

        logger.info(
            "[%s] SKILL COMPLETE — rollback_service_revision (sandbox) | "
            "service=%s recovered=%s is_real_mutation=%s",
            exec_id, service_name, recovered, recovered,
        )
        return result

    # ═══════════════════════════════════════════════════════════════════
    # SIMULATED MODE (default) — deterministic, no network IO
    # ═══════════════════════════════════════════════════════════════════
    # NOTE: victim_url presence alone does NOT trigger real execution.
    # Only MUHAFIZ_EXECUTION_MODE=sandbox enables real mutations.
    await asyncio.sleep(0.05)

    result = {
        "status": "success",
        "execution_id": exec_id,
        "timestamp": _now_iso(),
        "service": service_name,
        "adapter": "simulated",
        "is_real_mutation": False,
        "detail": {
            "operation": "rollback",
            "target_revision": target_revision,
            "execution_mode": "simulated",
            "command_simulated": (
                f"gcloud run services update-traffic {service_name} "
                f"--to-revisions={target_revision}=100"
            ),
            "previous_revision": "current",
            "rollback_completed": True,
        },
    }

    logger.info(
        "[%s] SKILL COMPLETE — rollback_service_revision (simulated) | service=%s → %s",
        exec_id, service_name, target_revision,
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# Skill 2 — API Gateway Rate Limiting
# ════════════════════════════════════════════════════════════════════════════

async def apply_rate_limit(
    service_name: str,
    requests_per_second: int,
    duration_seconds: int = 300,
    **kwargs: Any,
) -> dict[str, Any]:
    """Simulate applying rate-limiting rules to an API gateway.

    Configures a token-bucket rate limiter in front of the service to
    throttle abusive or unexpected traffic spikes.

    Args:
        service_name: Service whose gateway will be rate-limited.
        requests_per_second: Sustained request rate allowed (1–1000).
        duration_seconds: How long the limit is active (30–900 s).
        **kwargs: Absorbed for forward-compatibility.

    Returns:
        SkillResult-compatible dict with rate-limit configuration.
    """
    exec_id = _execution_id()
    logger.info(
        "[%s] SKILL START — apply_rate_limit | service=%s rps=%s duration=%ss",
        exec_id, service_name, requests_per_second, duration_seconds,
    )

    # ── Input validation ────────────────────────────────────────────────
    if not service_name or not service_name.strip():
        logger.error("[%s] service_name is empty", exec_id)
        return _error_result(exec_id, "", "service_name must be a non-empty string.")

    if not isinstance(requests_per_second, int) or requests_per_second <= 0:
        return _error_result(
            exec_id, service_name,
            f"requests_per_second must be a positive integer, got {requests_per_second}.",
        )

    if not isinstance(duration_seconds, int) or duration_seconds <= 0:
        return _error_result(
            exec_id, service_name,
            f"duration_seconds must be a positive integer, got {duration_seconds}.",
        )

    # ── Simulate gateway configuration ──────────────────────────────────
    await asyncio.sleep(0.05)

    rate_limit_config = {
        "requests_per_second": requests_per_second,
        "duration_seconds": duration_seconds,
        "algorithm": "token_bucket",
        "scope": "per_client_ip",
    }

    result: dict[str, Any] = {
        "status": "success",
        "execution_id": exec_id,
        "timestamp": _now_iso(),
        "service": service_name,
        "adapter": "simulated",
        "is_real_mutation": False,
        "detail": {
            "operation": "apply_rate_limit",
            "rate_limit_config": rate_limit_config,
            "estimated_daily_capacity": requests_per_second * 86_400,
            "command_simulated": (
                f"gcloud api-gateway rate-limit set {service_name} "
                f"--rps={requests_per_second} --duration={duration_seconds}s"
            ),
        },
    }

    logger.info(
        "[%s] SKILL COMPLETE — apply_rate_limit | service=%s rps=%d duration=%ds",
        exec_id, service_name, requests_per_second, duration_seconds,
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# Skill 3 — Autoscaler Update
# ════════════════════════════════════════════════════════════════════════════

async def scale_service(
    service_name: str,
    replicas: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Simulate adjusting the autoscaler replica count for a service.

    Maps to Cloud Run ``--min-instances`` / ``--max-instances`` or
    Kubernetes HPA ``--replicas`` depending on the deployment target.

    Args:
        service_name: Service to scale.
        replicas: Target replica count (bounded 1–6 by models).
        **kwargs: Absorbed for forward-compatibility.

    Returns:
        SkillResult-compatible dict with scaling details.
    """
    exec_id = _execution_id()
    logger.info(
        "[%s] SKILL START — scale_service | service=%s replicas=%s",
        exec_id, service_name, replicas,
    )

    # ── Input validation ────────────────────────────────────────────────
    if not service_name or not service_name.strip():
        logger.error("[%s] service_name is empty", exec_id)
        return _error_result(exec_id, "", "service_name must be a non-empty string.")

    if not isinstance(replicas, int) or replicas < 1:
        return _error_result(
            exec_id, service_name,
            f"replicas must be a positive integer, got {replicas}.",
        )

    # ── Simulate autoscaler update ──────────────────────────────────────
    await asyncio.sleep(0.05)

    result: dict[str, Any] = {
        "status": "success",
        "execution_id": exec_id,
        "timestamp": _now_iso(),
        "service": service_name,
        "adapter": "simulated",
        "is_real_mutation": False,
        "detail": {
            "operation": "scale_service",
            "replicas": replicas,
            "previous_replicas": "auto",
            "command_simulated": (
                f"gcloud run services update {service_name} "
                f"--min-instances={replicas} --max-instances={replicas}"
            ),
            "scaling_completed": True,
        },
    }

    logger.info(
        "[%s] SKILL COMPLETE — scale_service | service=%s replicas=%d",
        exec_id, service_name, replicas,
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# Skill 4 — Redis Cache Flush
# ════════════════════════════════════════════════════════════════════════════

async def flush_cache(
    service_name: str,
    cache_type: str = "all",
    **kwargs: Any,
) -> dict[str, Any]:
    """Simulate flushing the cache layer for a service.

    Supports targeted flush (e.g. ``'redis'``, ``'jwks'``) or full
    ``'all'`` flush.  Targeted flushes limit blast radius — only stale
    or poisoned keys are evicted.

    Args:
        service_name: Service whose cache to flush.
        cache_type: Cache type to flush (``'redis'``, ``'jwks'``, ``'all'``).
        **kwargs: Absorbed for forward-compatibility.

    Returns:
        SkillResult-compatible dict with flush details.
    """
    exec_id = _execution_id()
    logger.info(
        "[%s] SKILL START — flush_cache | service=%s cache_type=%s",
        exec_id, service_name, cache_type,
    )

    # ── Input validation ────────────────────────────────────────────────
    if not service_name or not service_name.strip():
        logger.error("[%s] service_name is empty", exec_id)
        return _error_result(exec_id, "", "service_name must be a non-empty string.")

    if not cache_type or not cache_type.strip():
        cache_type = "all"

    # ── Simulate cache flush ────────────────────────────────────────────
    await asyncio.sleep(0.05)

    # Deterministic key count seeded from service + type for reproducibility
    seed = int(
        hashlib.md5(f"{service_name}:{cache_type}".encode()).hexdigest()[:8],
        16,
    )
    keys_flushed = (seed % 5000) + 100

    result: dict[str, Any] = {
        "status": "success",
        "execution_id": exec_id,
        "timestamp": _now_iso(),
        "service": service_name,
        "adapter": "simulated",
        "is_real_mutation": False,
        "detail": {
            "operation": "flush_cache",
            "cache_type": cache_type,
            "keys_flushed": keys_flushed,
            "command_simulated": (
                f"redis-cli -h {service_name}-redis FLUSHDB"
                if cache_type == "all"
                else f"redis-cli -h {service_name}-redis --scan --pattern '{cache_type}:*' | xargs redis-cli DEL"
            ),
            "flush_completed": True,
        },
    }

    logger.info(
        "[%s] SKILL COMPLETE — flush_cache | service=%s type=%s keys=%d",
        exec_id, service_name, cache_type, keys_flushed,
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# Skill 5 — Secret Manager Credential Rotation
# ════════════════════════════════════════════════════════════════════════════

async def rotate_credentials(
    service_name: str,
    credential_type: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Simulate rotating secrets/API keys via Secret Manager.

    Lifecycle:
        1. Generate new credential version in Secret Manager.
        2. Update service environment to reference the new version.
        3. Mark old version as DISABLED (kept for audit trail).

    Args:
        service_name: Service whose credentials are rotated.
        credential_type: Type of credential (``'api_key'``, ``'db_password'``,
            ``'service_account'``, ``'tls_cert'``).
        **kwargs: Absorbed for forward-compatibility.

    Returns:
        SkillResult-compatible dict with rotation details.
    """
    exec_id = _execution_id()
    valid_types = {"api_key", "db_password", "jwt_signing_key", "service_account", "tls_cert"}

    logger.info(
        "[%s] SKILL START — rotate_credentials | service=%s type=%s",
        exec_id, service_name, credential_type,
    )

    # ── Input validation ────────────────────────────────────────────────
    if not service_name or not service_name.strip():
        logger.error("[%s] service_name is empty", exec_id)
        return _error_result(exec_id, "", "service_name must be a non-empty string.")

    if not credential_type or not credential_type.strip():
        return _error_result(
            exec_id, service_name,
            "credential_type must be a non-empty string.",
        )

    # Normalize common credential_type aliases the model might produce
    _CREDENTIAL_ALIASES: dict[str, str] = {
        "jwt": "jwt_signing_key",
        "jwt_key": "jwt_signing_key",
        "signing_key": "jwt_signing_key",
        "api-key": "api_key",
        "apikey": "api_key",
        "tls": "tls_cert",
        "ssl_cert": "tls_cert",
        "db_pass": "db_password",
        "database_password": "db_password",
    }
    credential_type = _CREDENTIAL_ALIASES.get(credential_type, credential_type)

    if credential_type not in valid_types:
        return _error_result(
            exec_id, service_name,
            f"Unknown credential_type '{credential_type}'. "
            f"Must be one of {sorted(valid_types)}.",
        )

    # ── Simulate Secret Manager rotation ────────────────────────────────
    await asyncio.sleep(0.05)

    secret_name = f"{service_name}-{credential_type.replace('_', '-')}"
    old_version = "v6"
    new_version = "v7"

    result: dict[str, Any] = {
        "status": "success",
        "execution_id": exec_id,
        "timestamp": _now_iso(),
        "service": service_name,
        "adapter": "simulated",
        "is_real_mutation": False,
        "detail": {
            "operation": "rotate_credentials",
            "credential_type": credential_type,
            "secret_name": secret_name,
            "old_version": old_version,
            "old_version_status": "DISABLED",
            "new_version": new_version,
            "new_version_status": "ENABLED",
            "command_simulated": (
                f"gcloud secrets versions add {secret_name} --data-file=-"
            ),
            "rotation_completed": True,
        },
    }

    logger.info(
        "[%s] SKILL COMPLETE — rotate_credentials | %s rotated %s → %s",
        exec_id, secret_name, old_version, new_version,
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# Skill 6 — Kubernetes Rollout Restart
# ════════════════════════════════════════════════════════════════════════════

async def restart_service(
    service_name: str,
    graceful: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Simulate a Kubernetes rolling restart of a Deployment.

    Equivalent of::

        kubectl rollout restart deployment/<service> -n default

    A **graceful** restart (default) replaces pods one-by-one with
    zero downtime.  A non-graceful restart terminates all pods
    simultaneously — useful when in-memory state is corrupted.

    Args:
        service_name: Kubernetes Deployment name.
        graceful: If ``True``, perform rolling restart; if ``False``,
            terminate all pods simultaneously.
        **kwargs: Absorbed for forward-compatibility.

    Returns:
        SkillResult-compatible dict with restart details.
    """
    exec_id = _execution_id()
    strategy = "rolling" if graceful else "simultaneous"

    logger.info(
        "[%s] SKILL START — restart_service | service=%s graceful=%s strategy=%s",
        exec_id, service_name, graceful, strategy,
    )

    # ── Input validation ────────────────────────────────────────────────
    if not service_name or not service_name.strip():
        logger.error("[%s] service_name is empty", exec_id)
        return _error_result(exec_id, "", "service_name must be a non-empty string.")

    # ── Simulate kubectl rollout ────────────────────────────────────────
    await asyncio.sleep(0.05)

    # Simulate a 3-replica deployment restart
    simulated_pods = [
        {
            "pod": f"{service_name}-{uuid.uuid4().hex[:5]}",
            "status": "Terminating → Running",
            "restart_order": i + 1,
        }
        for i in range(3)
    ]

    if graceful:
        command = f"kubectl rollout restart deployment/{service_name} -n default"
    else:
        command = (
            f"kubectl delete pods -l app={service_name} "
            f"-n default --grace-period=0"
        )

    result: dict[str, Any] = {
        "status": "success",
        "execution_id": exec_id,
        "timestamp": _now_iso(),
        "service": service_name,
        "adapter": "simulated",
        "is_real_mutation": False,
        "detail": {
            "operation": "restart_service",
            "restart_strategy": strategy,
            "graceful": graceful,
            "pods_affected": simulated_pods,
            "pods_restarted": len(simulated_pods),
            "command_simulated": command,
            "restart_completed": True,
        },
    }

    logger.info(
        "[%s] SKILL COMPLETE — restart_service | service=%s %d pods restarted (%s)",
        exec_id, service_name, len(simulated_pods), strategy,
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# Skill Registry (§16)
# ════════════════════════════════════════════════════════════════════════════

SKILL_REGISTRY: dict[str, Callable[..., Any]] = {
    AllowedSkill.ROLLBACK_SERVICE_REVISION.value: rollback_service_revision,
    AllowedSkill.APPLY_RATE_LIMIT.value: apply_rate_limit,
    AllowedSkill.SCALE_SERVICE.value: scale_service,
    AllowedSkill.FLUSH_CACHE.value: flush_cache,
    AllowedSkill.ROTATE_CREDENTIALS.value: rotate_credentials,
    AllowedSkill.RESTART_SERVICE.value: restart_service,
}


# ════════════════════════════════════════════════════════════════════════════
# Single Skill Dispatcher
# ════════════════════════════════════════════════════════════════════════════

async def execute_skill(
    skill_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Look up a skill in the registry and execute it.

    Args:
        skill_name: The skill's string name (must be in ``SKILL_REGISTRY``).
        arguments: Keyword arguments to pass to the skill function.

    Returns:
        SkillResult-compatible dict from the skill function.

    Raises:
        ValueError: If ``skill_name`` is not in the registry.
    """
    skill_fn = SKILL_REGISTRY.get(skill_name)
    if skill_fn is None:
        raise ValueError(
            f"Unknown skill {skill_name!r}. "
            f"Available skills: {sorted(SKILL_REGISTRY.keys())}"
        )

    logger.info("execute_skill: dispatching %s with args=%s", skill_name, arguments)

    result = await skill_fn(**arguments)
    logger.info(
        "execute_skill: %s completed with status=%s",
        skill_name, result.get("status", "unknown"),
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# Action Graph Executor (§20)
# ════════════════════════════════════════════════════════════════════════════

async def execute_action_graph(
    actions: list[dict[str, Any]],
    store: Any | None = None,
    incident_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute a list of action envelopes in topological dependency order.

    This function:
        1. Parses raw dicts into ``ActionEnvelope`` objects.
        2. Sorts by ``topological_sort`` (raises on cycles).
        3. Checks eligibility via ``check_action_eligibility``.
        4. Dispatches each action through ``execute_skill``.
        5. Respects ``on_failure`` policies (STOP vs CONTINUE).
        6. Optionally persists execution events to the IncidentStore.
        7. Returns a full reconciliation dict.

    Args:
        actions: List of raw action dicts (ActionEnvelope-compatible).
        store: Optional ``IncidentStore`` for event persistence.
        incident_id: Incident context for event logging.
        run_id: Pipeline run context for event logging.

    Returns:
        Reconciliation dict with:
            - ``status``: ``'all_succeeded'``, ``'partial'``, or ``'all_failed'``
            - ``total``: Number of actions attempted
            - ``succeeded``: Count of successful actions
            - ``failed``: Count of failed actions
            - ``skipped``: Count of skipped actions
            - ``receipts``: Map of action_id → execution receipt
            - ``execution_order``: Ordered list of action IDs as executed
            - ``timestamp``: ISO-8601 completion timestamp
    """
    from gateway.models import ActionEnvelope as AE

    logger.info(
        "execute_action_graph: starting with %d action(s) | incident=%s run=%s",
        len(actions), incident_id, run_id,
    )

    # ── Parse raw dicts into ActionEnvelope models ──────────────────────
    envelopes: list[AE] = []
    for raw in actions:
        if isinstance(raw, AE):
            envelopes.append(raw)
        else:
            envelopes.append(AE(**raw))

    # ── Topological sort (raises ValueError on cycle) ───────────────────
    try:
        sorted_actions = topological_sort(envelopes)
    except ValueError as exc:
        logger.error("execute_action_graph: topological sort failed: %s", exc)
        return {
            "status": "all_failed",
            "total": len(envelopes),
            "succeeded": 0,
            "failed": len(envelopes),
            "skipped": 0,
            "receipts": {},
            "execution_order": [],
            "timestamp": _now_iso(),
            "error": str(exc),
        }

    # ── Execute in order ────────────────────────────────────────────────
    receipts: dict[str, dict[str, Any]] = {}
    execution_order: list[str] = []
    succeeded = 0
    failed = 0
    skipped = 0
    halt = False  # Set to True when a STOP-policy action fails

    for action in sorted_actions:
        action_id = action.action_id

        # Check if we're halted due to a prior STOP failure
        if halt:
            logger.warning(
                "execute_action_graph: skipping %s (halted by prior STOP failure)",
                action_id,
            )
            receipts[action_id] = {
                "status": "skipped",
                "reason": "halted_by_prior_failure",
                "timestamp": _now_iso(),
            }
            skipped += 1
            continue

        # Check eligibility (dependencies satisfied?)
        eligible, reason = check_action_eligibility(
            action, receipts, sorted_actions,
        )
        if not eligible:
            logger.warning(
                "execute_action_graph: %s not eligible: %s", action_id, reason,
            )
            receipts[action_id] = {
                "status": "skipped",
                "reason": reason,
                "timestamp": _now_iso(),
            }
            skipped += 1
            continue

        # Dispatch the skill
        skill_name = action.skill.value
        arguments = dict(action.arguments)

        # Ensure service_name is in arguments (from action.target)
        if "service_name" not in arguments:
            arguments["service_name"] = action.target

        try:
            receipt = await execute_skill(skill_name, arguments)
        except Exception as exc:
            logger.error(
                "execute_action_graph: %s raised exception: %s",
                action_id, exc,
            )
            receipt = {
                "status": "error",
                "execution_id": _execution_id(),
                "timestamp": _now_iso(),
                "service": action.target,
                "adapter": "simulated",
                "detail": {"error": str(exc)},
            }

        receipts[action_id] = receipt
        execution_order.append(action_id)

        # Persist event to store if available
        if store is not None and incident_id and run_id:
            try:
                await store.append_event(
                    incident_id=incident_id,
                    run_id=run_id,
                    actor="aamil",
                    actor_role="executor",
                    event_type="skill_executed",
                    summary=f"Executed {skill_name} on {action.target}: {receipt.get('status')}",
                    payload={
                        "action_id": action_id,
                        "skill": skill_name,
                        "target": action.target,
                        "receipt": receipt,
                    },
                )
            except Exception as evt_exc:
                logger.warning(
                    "execute_action_graph: failed to persist event for %s: %s",
                    action_id, evt_exc,
                )

        # Tally result and check failure policy
        if receipt.get("status") == "success":
            succeeded += 1
        else:
            failed += 1
            if action.on_failure == FailurePolicy.STOP:
                logger.error(
                    "execute_action_graph: %s failed with STOP policy — halting graph",
                    action_id,
                )
                halt = True
            else:
                logger.warning(
                    "execute_action_graph: %s failed with CONTINUE policy — proceeding",
                    action_id,
                )

    # ── Build reconciliation ────────────────────────────────────────────
    total = succeeded + failed + skipped
    if total == 0 or failed == total:
        overall_status = "all_failed"
    elif failed == 0 and skipped == 0:
        overall_status = "all_succeeded"
    else:
        overall_status = "partial"

    reconciliation: dict[str, Any] = {
        "status": overall_status,
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "receipts": receipts,
        "execution_order": execution_order,
        "timestamp": _now_iso(),
    }

    logger.info(
        "execute_action_graph: completed | status=%s succeeded=%d failed=%d skipped=%d",
        overall_status, succeeded, failed, skipped,
    )
    return reconciliation
