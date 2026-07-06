"""
tests/test_skills.py – Unit Tests for Skill Adapters
============================================================

Tests the async skill adapter layer (shared/skills.py):
    - Each of the 6 skills returns the correct SkillResult shape
    - SKILL_REGISTRY contains all 6 entries
    - execute_skill dispatches correctly
    - execute_skill with unknown skill raises ValueError
    - Skill validation (empty service_name returns error result)
"""


import pytest

from gateway.models import AllowedSkill
from shared.skills import (
    SKILL_REGISTRY,
    apply_rate_limit,
    execute_skill,
    flush_cache,
    restart_service,
    rollback_service_revision,
    rotate_credentials,
    scale_service,
)

# Required keys in every SkillResult-compatible dict
RESULT_KEYS = {"status", "execution_id", "timestamp", "service", "detail", "adapter"}


# ═══════════════════════════════════════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════════════════════════════════════

def _assert_result_shape(result: dict) -> None:
    """Assert that a result dict has all required SkillResult keys."""
    for key in RESULT_KEYS:
        assert key in result, f"Missing key {key!r} in result"
    assert result["adapter"] == "simulated"
    assert result["status"] in ("success", "error")
    assert isinstance(result["execution_id"], str)
    assert len(result["execution_id"]) == 8
    assert isinstance(result["timestamp"], str)
    assert isinstance(result["detail"], dict)


# ═══════════════════════════════════════════════════════════════════════════════
# Individual Skill Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestRollbackServiceRevision:
    """Tests for rollback_service_revision skill."""

    @pytest.mark.asyncio
    async def test_returns_correct_shape(self):
        result = await rollback_service_revision(
            service_name="auth-service",
            target_revision="rev-001",
        )
        _assert_result_shape(result)
        assert result["status"] == "success"
        assert result["service"] == "auth-service"
        assert result["detail"]["operation"] == "rollback"
        assert result["detail"]["target_revision"] == "rev-001"

    @pytest.mark.asyncio
    async def test_empty_service_name_returns_error(self):
        result = await rollback_service_revision(
            service_name="",
            target_revision="rev-001",
        )
        _assert_result_shape(result)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_empty_revision_returns_error(self):
        result = await rollback_service_revision(
            service_name="auth-service",
            target_revision="",
        )
        _assert_result_shape(result)
        assert result["status"] == "error"


class TestApplyRateLimit:
    """Tests for apply_rate_limit skill."""

    @pytest.mark.asyncio
    async def test_returns_correct_shape(self):
        result = await apply_rate_limit(
            service_name="payment-gateway",
            requests_per_second=100,
            duration_seconds=300,
        )
        _assert_result_shape(result)
        assert result["status"] == "success"
        assert result["service"] == "payment-gateway"
        assert result["detail"]["operation"] == "apply_rate_limit"

    @pytest.mark.asyncio
    async def test_empty_service_name_returns_error(self):
        result = await apply_rate_limit(
            service_name="",
            requests_per_second=100,
        )
        _assert_result_shape(result)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_rps_returns_error(self):
        result = await apply_rate_limit(
            service_name="payment-gateway",
            requests_per_second=-1,
        )
        _assert_result_shape(result)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_duration_returns_error(self):
        result = await apply_rate_limit(
            service_name="payment-gateway",
            requests_per_second=100,
            duration_seconds=0,
        )
        _assert_result_shape(result)
        assert result["status"] == "error"


class TestScaleService:
    """Tests for scale_service skill."""

    @pytest.mark.asyncio
    async def test_returns_correct_shape(self):
        result = await scale_service(
            service_name="user-service",
            replicas=3,
        )
        _assert_result_shape(result)
        assert result["status"] == "success"
        assert result["service"] == "user-service"
        assert result["detail"]["operation"] == "scale_service"
        assert result["detail"]["replicas"] == 3

    @pytest.mark.asyncio
    async def test_empty_service_name_returns_error(self):
        result = await scale_service(service_name="", replicas=2)
        _assert_result_shape(result)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_zero_replicas_returns_error(self):
        result = await scale_service(service_name="auth-service", replicas=0)
        _assert_result_shape(result)
        assert result["status"] == "error"


class TestFlushCache:
    """Tests for flush_cache skill."""

    @pytest.mark.asyncio
    async def test_returns_correct_shape(self):
        result = await flush_cache(
            service_name="payment-gateway",
            cache_type="redis",
        )
        _assert_result_shape(result)
        assert result["status"] == "success"
        assert result["service"] == "payment-gateway"
        assert result["detail"]["operation"] == "flush_cache"
        assert result["detail"]["cache_type"] == "redis"

    @pytest.mark.asyncio
    async def test_default_cache_type(self):
        result = await flush_cache(service_name="auth-service")
        _assert_result_shape(result)
        assert result["status"] == "success"
        assert result["detail"]["cache_type"] == "all"

    @pytest.mark.asyncio
    async def test_empty_service_name_returns_error(self):
        result = await flush_cache(service_name="")
        _assert_result_shape(result)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_deterministic_keys_flushed(self):
        """Same inputs should produce same keys_flushed count."""
        r1 = await flush_cache(service_name="auth-service", cache_type="redis")
        r2 = await flush_cache(service_name="auth-service", cache_type="redis")
        assert r1["detail"]["keys_flushed"] == r2["detail"]["keys_flushed"]


class TestRotateCredentials:
    """Tests for rotate_credentials skill."""

    @pytest.mark.asyncio
    async def test_returns_correct_shape(self):
        result = await rotate_credentials(
            service_name="auth-service",
            credential_type="api_key",
        )
        _assert_result_shape(result)
        assert result["status"] == "success"
        assert result["service"] == "auth-service"
        assert result["detail"]["operation"] == "rotate_credentials"
        assert result["detail"]["credential_type"] == "api_key"
        assert result["detail"]["rotation_completed"] is True

    @pytest.mark.asyncio
    async def test_empty_service_name_returns_error(self):
        result = await rotate_credentials(
            service_name="",
            credential_type="api_key",
        )
        _assert_result_shape(result)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_invalid_credential_type_returns_error(self):
        result = await rotate_credentials(
            service_name="auth-service",
            credential_type="invalid_type",
        )
        _assert_result_shape(result)
        assert result["status"] == "error"
        assert "Unknown credential_type" in result["detail"]["error"]

    @pytest.mark.asyncio
    async def test_all_valid_credential_types(self):
        """All four credential types should succeed."""
        for cred_type in ("api_key", "db_password", "service_account", "tls_cert"):
            result = await rotate_credentials(
                service_name="auth-service",
                credential_type=cred_type,
            )
            assert result["status"] == "success", f"Failed for {cred_type}"


class TestRestartService:
    """Tests for restart_service skill."""

    @pytest.mark.asyncio
    async def test_returns_correct_shape_graceful(self):
        result = await restart_service(
            service_name="auth-service",
            graceful=True,
        )
        _assert_result_shape(result)
        assert result["status"] == "success"
        assert result["service"] == "auth-service"
        assert result["detail"]["operation"] == "restart_service"
        assert result["detail"]["restart_strategy"] == "rolling"
        assert result["detail"]["graceful"] is True

    @pytest.mark.asyncio
    async def test_non_graceful_restart(self):
        result = await restart_service(
            service_name="auth-service",
            graceful=False,
        )
        _assert_result_shape(result)
        assert result["status"] == "success"
        assert result["detail"]["restart_strategy"] == "simultaneous"
        assert result["detail"]["graceful"] is False

    @pytest.mark.asyncio
    async def test_empty_service_name_returns_error(self):
        result = await restart_service(service_name="")
        _assert_result_shape(result)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_pods_count(self):
        """Should simulate 3 pods being restarted."""
        result = await restart_service(service_name="auth-service")
        assert result["detail"]["pods_restarted"] == 3
        assert len(result["detail"]["pods_affected"]) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Registry Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkillRegistry:
    """Tests for SKILL_REGISTRY completeness."""

    def test_registry_has_all_six_skills(self):
        """SKILL_REGISTRY should contain all 6 AllowedSkill entries."""
        expected_skills = {s.value for s in AllowedSkill}
        assert set(SKILL_REGISTRY.keys()) == expected_skills

    def test_registry_size(self):
        """Registry should have exactly 6 entries."""
        assert len(SKILL_REGISTRY) == 6

    def test_registry_values_are_callable(self):
        """Every registry value should be a callable."""
        for name, fn in SKILL_REGISTRY.items():
            assert callable(fn), f"Registry entry {name!r} is not callable"


# ═══════════════════════════════════════════════════════════════════════════════
# execute_skill Dispatch Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecuteSkill:
    """Tests for execute_skill dispatcher."""

    @pytest.mark.asyncio
    async def test_dispatches_rollback(self):
        """execute_skill should dispatch to rollback_service_revision."""
        result = await execute_skill(
            "rollback_service_revision",
            {"service_name": "auth-service", "target_revision": "rev-01"},
        )
        _assert_result_shape(result)
        assert result["status"] == "success"
        assert result["detail"]["operation"] == "rollback"

    @pytest.mark.asyncio
    async def test_dispatches_scale(self):
        """execute_skill should dispatch to scale_service."""
        result = await execute_skill(
            "scale_service",
            {"service_name": "user-service", "replicas": 2},
        )
        _assert_result_shape(result)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_unknown_skill_raises_value_error(self):
        """execute_skill should raise ValueError for unknown skills."""
        with pytest.raises(ValueError, match="Unknown skill"):
            await execute_skill("destroy_everything", {})

    @pytest.mark.asyncio
    async def test_dispatches_all_skills(self):
        """execute_skill should dispatch all 6 registered skills."""
        test_args = {
            "rollback_service_revision": {
                "service_name": "auth-service",
                "target_revision": "rev-01",
            },
            "apply_rate_limit": {
                "service_name": "auth-service",
                "requests_per_second": 50,
            },
            "scale_service": {
                "service_name": "auth-service",
                "replicas": 2,
            },
            "flush_cache": {
                "service_name": "auth-service",
            },
            "rotate_credentials": {
                "service_name": "auth-service",
                "credential_type": "api_key",
            },
            "restart_service": {
                "service_name": "auth-service",
            },
        }
        for skill_name, args in test_args.items():
            result = await execute_skill(skill_name, args)
            _assert_result_shape(result)
            assert result["status"] == "success", f"Failed for {skill_name}"
