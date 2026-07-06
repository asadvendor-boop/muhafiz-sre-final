"""
tests/test_models.py – Unit Tests for Domain Models
==========================================================

Tests the Pydantic domain models, enums, and utility functions:
    - Alert creation and validation (valid and invalid service_ids)
    - ActionEnvelope creation with all skill types
    - MitigationPlan with action list
    - EvaluationScenario instantiation
    - canonical_json determinism
    - sha256_hex consistency
    - Enum values match expectations
"""

import json

import pytest
from pydantic import ValidationError

from gateway.models import (
    Alert,
    Severity,
    AllowedSkill,
    AllowedService,
    FailurePolicy,
    IncidentStatus,
    RootCauseCode,
    RiskLevel,
    ActionEnvelope,
    MitigationPlan,
    EvaluationScenario,
    HumanPolicy,
    RecoveryOracle,
    canonical_json,
    sha256_hex,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Alert Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlert:
    """Tests for the Alert model."""

    def test_valid_alert_creation(self):
        """A well-formed alert should construct without errors."""
        alert = Alert(
            severity=Severity.P1,
            service_id="auth-service",
            summary="Error rate spike after deployment",
            alert_type="error_rate",
            error_message="JWT validation failures",
        )
        assert alert.severity == Severity.P1
        assert alert.service_id == "auth-service"
        assert alert.summary == "Error rate spike after deployment"
        assert alert.alert_type == "error_rate"
        assert alert.timestamp  # auto-generated

    def test_alert_default_fields(self):
        """Defaults should be applied for alert_type and error_message."""
        alert = Alert(
            severity=Severity.P2,
            service_id="payment-gateway",
            summary="Latency spike detected",
        )
        assert alert.alert_type == "generic"
        assert alert.error_message == ""

    def test_alert_service_id_sanitisation_strips(self):
        """Leading/trailing whitespace should be stripped from service_id."""
        alert = Alert(
            severity=Severity.P3,
            service_id="  auth-service  ",
            summary="Test",
        )
        assert alert.service_id == "auth-service"

    def test_alert_service_id_rejects_special_chars(self):
        """service_id with shell metacharacters should be rejected."""
        with pytest.raises(ValidationError, match="disallowed characters"):
            Alert(
                severity=Severity.P1,
                service_id="auth; rm -rf /",
                summary="Injection attempt",
            )

    def test_alert_service_id_rejects_spaces(self):
        """service_id with embedded spaces should be rejected."""
        with pytest.raises(ValidationError, match="disallowed characters"):
            Alert(
                severity=Severity.P1,
                service_id="auth service",
                summary="Invalid service id",
            )

    def test_alert_empty_service_id_rejected(self):
        """Empty string service_id should be rejected."""
        with pytest.raises(ValidationError):
            Alert(
                severity=Severity.P1,
                service_id="",
                summary="Empty service",
            )

    def test_alert_empty_summary_rejected(self):
        """Empty summary should be rejected (min_length=1)."""
        with pytest.raises(ValidationError):
            Alert(
                severity=Severity.P1,
                service_id="auth-service",
                summary="",
            )

    def test_alert_all_severities(self):
        """All Severity enum values should be accepted."""
        for sev in Severity:
            alert = Alert(
                severity=sev,
                service_id="auth-service",
                summary=f"Test {sev.value}",
            )
            assert alert.severity == sev

    def test_alert_service_id_allows_dots_underscores(self):
        """service_id with dots and underscores should be accepted."""
        alert = Alert(
            severity=Severity.P2,
            service_id="my_service.v2",
            summary="Dotted and underscored",
        )
        assert alert.service_id == "my_service.v2"


# ═══════════════════════════════════════════════════════════════════════════════
# ActionEnvelope Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestActionEnvelope:
    """Tests for the ActionEnvelope model with all skill types."""

    def test_rollback_envelope(self):
        """Create a rollback_service_revision action envelope."""
        env = ActionEnvelope(
            action_id="act-1",
            skill=AllowedSkill.ROLLBACK_SERVICE_REVISION,
            target="auth-service",
            arguments={"service_name": "auth-service", "target_revision": "rev-01"},
        )
        assert env.skill == AllowedSkill.ROLLBACK_SERVICE_REVISION
        assert env.target == "auth-service"
        assert env.on_failure == FailurePolicy.STOP  # default

    def test_rate_limit_envelope(self):
        """Create an apply_rate_limit action envelope."""
        env = ActionEnvelope(
            action_id="act-2",
            skill=AllowedSkill.APPLY_RATE_LIMIT,
            target="payment-gateway",
            arguments={
                "service_name": "payment-gateway",
                "requests_per_second": 100,
                "duration_seconds": 300,
            },
        )
        assert env.skill == AllowedSkill.APPLY_RATE_LIMIT

    def test_scale_service_envelope(self):
        """Create a scale_service action envelope."""
        env = ActionEnvelope(
            action_id="act-3",
            skill=AllowedSkill.SCALE_SERVICE,
            target="user-service",
            arguments={"service_name": "user-service", "replicas": 3},
        )
        assert env.skill == AllowedSkill.SCALE_SERVICE

    def test_flush_cache_envelope(self):
        """Create a flush_cache action envelope."""
        env = ActionEnvelope(
            action_id="act-4",
            skill=AllowedSkill.FLUSH_CACHE,
            target="payment-gateway",
            arguments={"service_name": "payment-gateway", "cache_type": "redis"},
        )
        assert env.skill == AllowedSkill.FLUSH_CACHE

    def test_rotate_credentials_envelope(self):
        """Create a rotate_credentials action envelope."""
        env = ActionEnvelope(
            action_id="act-5",
            skill=AllowedSkill.ROTATE_CREDENTIALS,
            target="auth-service",
            arguments={
                "service_name": "auth-service",
                "credential_type": "api_key",
            },
        )
        assert env.skill == AllowedSkill.ROTATE_CREDENTIALS

    def test_restart_service_envelope(self):
        """Create a restart_service action envelope."""
        env = ActionEnvelope(
            action_id="act-6",
            skill=AllowedSkill.RESTART_SERVICE,
            target="auth-service",
            arguments={"service_name": "auth-service", "graceful": True},
        )
        assert env.skill == AllowedSkill.RESTART_SERVICE

    def test_envelope_with_dependencies(self):
        """Action with depends_on should set dependency list."""
        env = ActionEnvelope(
            action_id="act-b",
            skill=AllowedSkill.RESTART_SERVICE,
            target="auth-service",
            depends_on=["act-a"],
        )
        assert env.depends_on == ["act-a"]

    def test_envelope_continue_policy(self):
        """Action with CONTINUE on_failure should be accepted."""
        env = ActionEnvelope(
            action_id="act-c",
            skill=AllowedSkill.FLUSH_CACHE,
            target="payment-gateway",
            on_failure=FailurePolicy.CONTINUE,
        )
        assert env.on_failure == FailurePolicy.CONTINUE

    def test_invalid_skill_rejected(self):
        """An invalid skill name should raise ValidationError."""
        with pytest.raises(ValidationError):
            ActionEnvelope(
                action_id="act-bad",
                skill="destroy_everything",
                target="auth-service",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# MitigationPlan Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestMitigationPlan:
    """Tests for the MitigationPlan model."""

    def test_plan_with_actions(self):
        """A plan with a list of actions should be valid."""
        actions = [
            ActionEnvelope(
                action_id="act-1",
                skill=AllowedSkill.FLUSH_CACHE,
                target="payment-gateway",
                arguments={"service_name": "payment-gateway"},
            ),
            ActionEnvelope(
                action_id="act-2",
                skill=AllowedSkill.SCALE_SERVICE,
                target="payment-gateway",
                arguments={"service_name": "payment-gateway", "replicas": 3},
                depends_on=["act-1"],
            ),
        ]
        plan = MitigationPlan(
            actions=actions,
            strategy_summary="Flush cache then scale up",
            risk_level=RiskLevel.MEDIUM,
        )
        assert len(plan.actions) == 2
        assert plan.revision == 1  # default
        assert plan.plan_id.startswith("PLAN-")
        assert plan.risk_level == RiskLevel.MEDIUM

    def test_plan_empty_actions(self):
        """A plan with no actions is valid (false-alarm path)."""
        plan = MitigationPlan(
            actions=[],
            strategy_summary="No action needed",
            risk_level=RiskLevel.LOW,
        )
        assert len(plan.actions) == 0

    def test_plan_empty_strategy_rejected(self):
        """A plan with empty strategy_summary should fail."""
        with pytest.raises(ValidationError):
            MitigationPlan(
                actions=[],
                strategy_summary="",
                risk_level=RiskLevel.LOW,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# EvaluationScenario Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluationScenario:
    """Tests for the EvaluationScenario model."""

    def test_scenario_instantiation(self):
        """A well-formed scenario should construct without errors."""
        scenario = EvaluationScenario(
            id="test_scenario",
            alert=Alert(
                severity=Severity.P1,
                service_id="auth-service",
                summary="Test alert",
            ),
            expected_terminal_state=IncidentStatus.RESOLVED,
            acceptable_root_cause_codes={RootCauseCode.BAD_DEPLOYMENT},
            required_tools={"get_cloud_logging_traces"},
            required_actions={AllowedSkill.ROLLBACK_SERVICE_REVISION},
            allowed_actions={AllowedSkill.ROLLBACK_SERVICE_REVISION},
            action_expected=True,
            recovery_applies=True,
            human_policy=HumanPolicy.AUTO_APPROVE,
            recovery_oracle=RecoveryOracle.VICTIM_HEALTH,
            scenario_id="test_scenario",
        )
        assert scenario.id == "test_scenario"
        assert scenario.expected_terminal_state == IncidentStatus.RESOLVED
        assert AllowedSkill.ROLLBACK_SERVICE_REVISION in scenario.required_actions

    def test_scenario_defaults(self):
        """Optional fields should have correct defaults."""
        scenario = EvaluationScenario(
            id="default_test",
            alert=Alert(
                severity=Severity.P4,
                service_id="user-service",
                summary="Default test",
            ),
            expected_terminal_state=IncidentStatus.FALSE_ALARM,
            scenario_id="default_test",
        )
        assert scenario.action_expected is True
        assert scenario.recovery_applies is True
        assert scenario.challenge_required is False
        assert scenario.minimum_plan_revision == 1
        assert scenario.recovery_oracle == RecoveryOracle.NONE


# ═══════════════════════════════════════════════════════════════════════════════
# canonical_json / sha256_hex Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCanonicalJson:
    """Tests for canonical_json determinism."""

    def test_sorted_keys(self):
        """Keys should be sorted alphabetically."""
        result = canonical_json({"z": 1, "a": 2, "m": 3})
        parsed = json.loads(result)
        assert list(parsed.keys()) == ["a", "m", "z"]

    def test_no_spaces_in_separators(self):
        """Compact separators (',' and ':') should be used."""
        result = canonical_json({"key": "value"})
        assert " " not in result

    def test_determinism(self):
        """Same input should always produce the same output."""
        data = {"b": [1, 2, 3], "a": {"nested": True}}
        r1 = canonical_json(data)
        r2 = canonical_json(data)
        assert r1 == r2

    def test_pydantic_model_serialisation(self):
        """Pydantic models should be serialised via model_dump."""
        alert = Alert(
            severity=Severity.P1,
            service_id="auth-service",
            summary="Test",
        )
        result = canonical_json(alert)
        parsed = json.loads(result)
        assert parsed["service_id"] == "auth-service"
        assert parsed["severity"] == "P1"


class TestSha256Hex:
    """Tests for sha256_hex consistency."""

    def test_consistent_hash(self):
        """Same input dict should produce the same hash."""
        data = {"key": "value", "num": 42}
        h1 = sha256_hex(data)
        h2 = sha256_hex(data)
        assert h1 == h2

    def test_hash_length(self):
        """SHA-256 hex digest should be 64 characters."""
        h = sha256_hex({"test": True})
        assert len(h) == 64

    def test_different_data_different_hash(self):
        """Different inputs should produce different hashes."""
        h1 = sha256_hex({"a": 1})
        h2 = sha256_hex({"a": 2})
        assert h1 != h2


# ═══════════════════════════════════════════════════════════════════════════════
# Enum Value Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnums:
    """Tests that enum values match the specification."""

    def test_severity_values(self):
        """Severity should have P0–P4."""
        expected = {"P0", "P1", "P2", "P3", "P4"}
        assert {s.value for s in Severity} == expected

    def test_allowed_skill_values(self):
        """AllowedSkill should contain exactly 6 skills."""
        expected = {
            "rollback_service_revision",
            "apply_rate_limit",
            "scale_service",
            "flush_cache",
            "rotate_credentials",
            "restart_service",
        }
        assert {s.value for s in AllowedSkill} == expected

    def test_allowed_service_values(self):
        """AllowedService should contain the 3 target services."""
        expected = {"auth-service", "payment-gateway", "user-service"}
        assert {s.value for s in AllowedService} == expected

    def test_failure_policy_values(self):
        """FailurePolicy should have STOP and CONTINUE."""
        assert {fp.value for fp in FailurePolicy} == {"STOP", "CONTINUE"}

    def test_incident_status_includes_key_states(self):
        """IncidentStatus should include all key lifecycle states."""
        values = {s.value for s in IncidentStatus}
        required = {
            "DETECTED", "ANALYZING", "PLANNING", "AWAITING_APPROVAL",
            "EXECUTING", "RESOLVED", "REJECTED", "FALSE_ALARM",
        }
        assert required.issubset(values)

    def test_root_cause_code_values(self):
        """RootCauseCode should include the classifications."""
        values = {r.value for r in RootCauseCode}
        required = {
            "BAD_DEPLOYMENT", "CACHE_STAMPEDE", "EXPIRED_CREDENTIAL",
            "FALSE_POSITIVE", "UNKNOWN",
        }
        assert required.issubset(values)

    def test_risk_level_values(self):
        """RiskLevel should include critical through low."""
        expected = {"critical", "high", "medium", "low"}
        assert {r.value for r in RiskLevel} == expected
