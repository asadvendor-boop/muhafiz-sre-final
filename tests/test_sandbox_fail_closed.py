"""
tests/test_sandbox_fail_closed.py – Fail-closed sandbox execution tests
=========================================================================

Validates that MUHAFIZ_EXECUTION_MODE=sandbox enforces strict fail-closed
behavior:
  1. Wrong target service → error receipt (not simulated success)
  2. No victim URL → error receipt (not simulated success)
  3. Victim timeout → error receipt
  4. Simulated mode ignores victim URL → simulated receipt
  5. Receipt provenance: is_real_mutation, adapter, before/after state
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.skills import rollback_service_revision


# ────────────────────────────────────────────────────────────────────────────
# Test 1: Wrong target service in sandbox → error
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sandbox_wrong_service_fails_closed(monkeypatch):
    """
    In sandbox mode, targeting a service other than the sandbox target
    must return an error receipt — never fall back to simulation.
    """
    monkeypatch.setenv("MUHAFIZ_EXECUTION_MODE", "sandbox")
    monkeypatch.setenv("MUHAFIZ_SANDBOX_TARGET_SERVICE", "auth-service")
    monkeypatch.setenv("VICTIM_SERVICE_URL", "http://localhost:9000")

    result = await rollback_service_revision(
        service_name="payment-service",
        target_revision="rev-v3",
    )

    assert result["status"] == "error", f"Expected error, got {result['status']}"
    assert result["adapter"] == "sandbox"
    assert result["is_real_mutation"] is False
    assert "not permitted" in result["detail"]["error"]


# ────────────────────────────────────────────────────────────────────────────
# Test 2: No victim URL in sandbox → error
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sandbox_no_url_fails_closed(monkeypatch):
    """
    Sandbox mode without VICTIM_SERVICE_URL must fail closed.
    """
    monkeypatch.setenv("MUHAFIZ_EXECUTION_MODE", "sandbox")
    monkeypatch.setenv("MUHAFIZ_SANDBOX_TARGET_SERVICE", "auth-service")
    monkeypatch.delenv("VICTIM_SERVICE_URL", raising=False)

    result = await rollback_service_revision(
        service_name="auth-service",
        target_revision="rev-v2",
        victim_url=None,  # explicitly None
    )

    assert result["status"] == "error", f"Expected error, got {result['status']}"
    assert result["adapter"] == "sandbox"
    assert result["is_real_mutation"] is False
    assert "VICTIM_SERVICE_URL" in result["detail"]["error"]


# ────────────────────────────────────────────────────────────────────────────
# Test 3: Victim timeout in sandbox → error
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sandbox_timeout_fails_closed(monkeypatch):
    """
    When the victim service times out, sandbox mode must fail closed.
    """
    import httpx

    monkeypatch.setenv("MUHAFIZ_EXECUTION_MODE", "sandbox")
    monkeypatch.setenv("MUHAFIZ_SANDBOX_TARGET_SERVICE", "auth-service")

    # Use a non-routable IP to trigger timeout
    result = await rollback_service_revision(
        service_name="auth-service",
        target_revision="rev-v2",
        victim_url="http://192.0.2.1:9999",  # TEST-NET, will timeout
    )

    assert result["status"] == "error", f"Expected error, got {result['status']}"
    assert result["adapter"] == "sandbox"
    assert result["is_real_mutation"] is False


# ────────────────────────────────────────────────────────────────────────────
# Test 4: Simulated mode ignores victim URL
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_simulated_mode_ignores_victim_url(monkeypatch):
    """
    In simulated mode, even if VICTIM_SERVICE_URL is set,
    the skill must NOT make any HTTP calls. Receipt must show
    adapter=simulated and is_real_mutation=False.
    """
    monkeypatch.setenv("MUHAFIZ_EXECUTION_MODE", "simulated")
    monkeypatch.setenv("VICTIM_SERVICE_URL", "http://localhost:9000")

    result = await rollback_service_revision(
        service_name="auth-service",
        target_revision="rev-v2",
    )

    assert result["status"] == "success"
    assert result["adapter"] == "simulated"
    assert result["is_real_mutation"] is False
    assert result["detail"]["execution_mode"] == "simulated"


# ────────────────────────────────────────────────────────────────────────────
# Test 5: Default mode (no env var) is simulated
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_mode_is_simulated(monkeypatch):
    """
    When MUHAFIZ_EXECUTION_MODE is not set, mode defaults to simulated.
    """
    monkeypatch.delenv("MUHAFIZ_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("VICTIM_SERVICE_URL", raising=False)

    result = await rollback_service_revision(
        service_name="auth-service",
        target_revision="rev-v2",
    )

    assert result["status"] == "success"
    assert result["adapter"] == "simulated"
    assert result["is_real_mutation"] is False


# ────────────────────────────────────────────────────────────────────────────
# Test 6: All simulated skills have is_real_mutation=False
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_simulated_skills_have_provenance(monkeypatch):
    """
    Every skill in the registry, when run in simulated mode,
    must include is_real_mutation=False in its receipt.
    """
    monkeypatch.setenv("MUHAFIZ_EXECUTION_MODE", "simulated")
    monkeypatch.delenv("VICTIM_SERVICE_URL", raising=False)

    from shared.skills import (
        rollback_service_revision,
        apply_rate_limit,
        scale_service,
        flush_cache,
        rotate_credentials,
        restart_service,
    )

    skills_and_args = [
        (rollback_service_revision, {"service_name": "auth-service", "target_revision": "v1"}),
        (apply_rate_limit, {"service_name": "auth-service", "requests_per_second": 100}),
        (scale_service, {"service_name": "auth-service", "replicas": 3}),
        (flush_cache, {"service_name": "auth-service"}),
        (rotate_credentials, {"service_name": "auth-service", "credential_type": "api_key"}),
        (restart_service, {"service_name": "auth-service"}),
    ]

    for skill_fn, kwargs in skills_and_args:
        result = await skill_fn(**kwargs)
        assert result.get("is_real_mutation") is False, (
            f"{skill_fn.__name__} missing is_real_mutation=False, got: "
            f"{result.get('is_real_mutation')}"
        )
        assert result["adapter"] == "simulated", (
            f"{skill_fn.__name__} adapter should be 'simulated', got: "
            f"{result['adapter']}"
        )
