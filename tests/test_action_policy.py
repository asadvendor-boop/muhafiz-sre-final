"""
tests/test_action_policy.py – Unit Tests for Action Policy
=================================================================

Tests the deterministic action validation layer (shared/action_policy.py):
    - validate_single_action with valid actions
    - validate_single_action rejects unknown skills
    - validate_single_action rejects unknown target services
    - validate_action_graph with valid graph
    - validate_action_graph detects circular dependencies
    - topological sort ordering
"""

import pytest

from gateway.models import (
    ActionEnvelope,
    AllowedSkill,
    FailurePolicy,
)
from shared.action_policy import (
    detect_cycle,
    topological_sort,
    validate_action_graph,
    validate_single_action,
    check_action_eligibility,
    _check_value_safety,
    sanitize_arguments,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_action(
    action_id: str = "act-1",
    skill: AllowedSkill = AllowedSkill.ROLLBACK_SERVICE_REVISION,
    target: str = "auth-service",
    arguments: dict | None = None,
    depends_on: list[str] | None = None,
    on_failure: FailurePolicy = FailurePolicy.STOP,
) -> ActionEnvelope:
    """Create an ActionEnvelope with sensible defaults."""
    return ActionEnvelope(
        action_id=action_id,
        skill=skill,
        target=target,
        arguments=arguments or {"service_name": target, "target_revision": "rev-01"},
        depends_on=depends_on or [],
        on_failure=on_failure,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# validate_single_action Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateSingleAction:
    """Tests for validate_single_action."""

    def test_valid_rollback_action(self):
        """A well-formed rollback action should pass validation."""
        action = _make_action()
        ok, errors = validate_single_action(action)
        assert ok is True
        assert errors == []

    def test_valid_scale_action(self):
        """A well-formed scale action should pass validation."""
        action = _make_action(
            action_id="act-scale",
            skill=AllowedSkill.SCALE_SERVICE,
            target="payment-gateway",
            arguments={"service_name": "payment-gateway", "replicas": 3},
        )
        ok, errors = validate_single_action(action)
        assert ok is True
        assert errors == []

    def test_valid_flush_cache_action(self):
        """A well-formed flush_cache action should pass validation."""
        action = _make_action(
            action_id="act-flush",
            skill=AllowedSkill.FLUSH_CACHE,
            target="user-service",
            arguments={"service_name": "user-service", "cache_type": "redis"},
        )
        ok, errors = validate_single_action(action)
        assert ok is True
        assert errors == []

    def test_rejects_unknown_target(self):
        """Target not in ALLOWED_TARGETS should fail validation."""
        action = _make_action(target="malicious-service")
        ok, errors = validate_single_action(action)
        assert ok is False
        assert any("not in the allowed target list" in e for e in errors)

    def test_rejects_self_dependency(self):
        """An action that depends on itself should fail."""
        action = _make_action(depends_on=["act-1"])
        ok, errors = validate_single_action(action)
        assert ok is False
        assert any("depends on itself" in e for e in errors)

    def test_rejects_shell_injection_in_arguments(self):
        """Arguments with shell metacharacters should fail."""
        action = _make_action(
            arguments={
                "service_name": "auth-service; rm -rf /",
                "target_revision": "rev-01",
            },
        )
        ok, errors = validate_single_action(action)
        assert ok is False
        assert any("Shell metacharacter" in e or "Command injection" in e for e in errors)

    def test_rejects_url_in_arguments(self):
        """Arguments containing URLs should fail."""
        action = _make_action(
            arguments={
                "service_name": "auth-service",
                "target_revision": "https://evil.com/payload",
            },
        )
        ok, errors = validate_single_action(action)
        assert ok is False
        assert any("URL" in e for e in errors)

    def test_rejects_path_traversal(self):
        """Arguments with ../ should fail."""
        action = _make_action(
            arguments={
                "service_name": "auth-service",
                "target_revision": "../../../etc/passwd",
            },
        )
        ok, errors = validate_single_action(action)
        assert ok is False
        assert any("traversal" in e.lower() for e in errors)


# ═══════════════════════════════════════════════════════════════════════════════
# validate_action_graph Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateActionGraph:
    """Tests for validate_action_graph."""

    def test_valid_linear_graph(self):
        """A linear A → B → C dependency chain should pass."""
        actions = [
            _make_action(action_id="act-a"),
            _make_action(
                action_id="act-b",
                skill=AllowedSkill.FLUSH_CACHE,
                arguments={"service_name": "auth-service", "cache_type": "all"},
                depends_on=["act-a"],
            ),
            _make_action(
                action_id="act-c",
                skill=AllowedSkill.RESTART_SERVICE,
                arguments={"service_name": "auth-service", "graceful": True},
                depends_on=["act-b"],
            ),
        ]
        ok, errors = validate_action_graph(actions)
        assert ok is True
        assert errors == []

    def test_valid_parallel_graph(self):
        """Independent parallel actions should pass."""
        actions = [
            _make_action(action_id="act-1"),
            _make_action(
                action_id="act-2",
                skill=AllowedSkill.FLUSH_CACHE,
                target="payment-gateway",
                arguments={"service_name": "payment-gateway", "cache_type": "redis"},
            ),
        ]
        ok, errors = validate_action_graph(actions)
        assert ok is True

    def test_detects_circular_dependency(self):
        """A → B → A cycle should be detected."""
        actions = [
            _make_action(action_id="act-a", depends_on=["act-b"]),
            _make_action(
                action_id="act-b",
                skill=AllowedSkill.FLUSH_CACHE,
                arguments={"service_name": "auth-service", "cache_type": "all"},
                depends_on=["act-a"],
            ),
        ]
        ok, errors = validate_action_graph(actions)
        assert ok is False
        assert any("cycle" in e.lower() for e in errors)

    def test_detects_three_way_cycle(self):
        """A → B → C → A three-way cycle should be detected."""
        actions = [
            _make_action(action_id="act-a", depends_on=["act-c"]),
            _make_action(
                action_id="act-b",
                skill=AllowedSkill.FLUSH_CACHE,
                arguments={"service_name": "auth-service", "cache_type": "all"},
                depends_on=["act-a"],
            ),
            _make_action(
                action_id="act-c",
                skill=AllowedSkill.RESTART_SERVICE,
                arguments={"service_name": "auth-service", "graceful": True},
                depends_on=["act-b"],
            ),
        ]
        ok, errors = validate_action_graph(actions)
        assert ok is False
        assert any("cycle" in e.lower() for e in errors)

    def test_detects_duplicate_action_ids(self):
        """Duplicate action IDs should be flagged."""
        actions = [
            _make_action(action_id="act-dup"),
            _make_action(
                action_id="act-dup",
                skill=AllowedSkill.FLUSH_CACHE,
                arguments={"service_name": "auth-service", "cache_type": "all"},
            ),
        ]
        ok, errors = validate_action_graph(actions)
        assert ok is False
        assert any("Duplicate" in e for e in errors)

    def test_detects_missing_dependency(self):
        """Dependency on a non-existent action should be flagged."""
        actions = [
            _make_action(action_id="act-x", depends_on=["act-nonexistent"]),
        ]
        ok, errors = validate_action_graph(actions)
        assert ok is False
        assert any("non-existent" in e for e in errors)

    def test_empty_graph_is_valid(self):
        """An empty action list should be valid."""
        ok, errors = validate_action_graph([])
        assert ok is True
        assert errors == []


# ═══════════════════════════════════════════════════════════════════════════════
# topological_sort Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestTopologicalSort:
    """Tests for topological_sort ordering."""

    def test_linear_chain_ordering(self):
        """A → B → C should produce [A, B, C] order."""
        actions = [
            _make_action(action_id="act-c", depends_on=["act-b"],
                         skill=AllowedSkill.RESTART_SERVICE,
                         arguments={"service_name": "auth-service", "graceful": True}),
            _make_action(action_id="act-a"),
            _make_action(action_id="act-b", depends_on=["act-a"],
                         skill=AllowedSkill.FLUSH_CACHE,
                         arguments={"service_name": "auth-service", "cache_type": "all"}),
        ]
        sorted_actions = topological_sort(actions)
        ids = [a.action_id for a in sorted_actions]
        assert ids.index("act-a") < ids.index("act-b")
        assert ids.index("act-b") < ids.index("act-c")

    def test_independent_actions(self):
        """Independent actions should all appear (order not constrained)."""
        actions = [
            _make_action(action_id="act-x"),
            _make_action(action_id="act-y",
                         skill=AllowedSkill.FLUSH_CACHE,
                         arguments={"service_name": "auth-service", "cache_type": "all"}),
        ]
        sorted_actions = topological_sort(actions)
        assert len(sorted_actions) == 2
        ids = {a.action_id for a in sorted_actions}
        assert ids == {"act-x", "act-y"}

    def test_cycle_raises_value_error(self):
        """topological_sort should raise ValueError on a cycle."""
        actions = [
            _make_action(action_id="act-a", depends_on=["act-b"]),
            _make_action(action_id="act-b", depends_on=["act-a"],
                         skill=AllowedSkill.FLUSH_CACHE,
                         arguments={"service_name": "auth-service", "cache_type": "all"}),
        ]
        with pytest.raises(ValueError, match="[Cc]ycle"):
            topological_sort(actions)

    def test_empty_list(self):
        """Sorting an empty list should return an empty list."""
        assert topological_sort([]) == []

    def test_single_action(self):
        """A single action should be returned as-is."""
        action = _make_action(action_id="solo")
        result = topological_sort([action])
        assert len(result) == 1
        assert result[0].action_id == "solo"


# ═══════════════════════════════════════════════════════════════════════════════
# detect_cycle Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectCycle:
    """Tests for the detect_cycle function."""

    def test_no_cycle(self):
        """A linear graph should return False."""
        actions = [
            _make_action(action_id="a"),
            _make_action(action_id="b", depends_on=["a"],
                         skill=AllowedSkill.FLUSH_CACHE,
                         arguments={"service_name": "auth-service", "cache_type": "all"}),
        ]
        assert detect_cycle(actions) is False

    def test_has_cycle(self):
        """A→B→A cycle should return True."""
        actions = [
            _make_action(action_id="a", depends_on=["b"]),
            _make_action(action_id="b", depends_on=["a"],
                         skill=AllowedSkill.FLUSH_CACHE,
                         arguments={"service_name": "auth-service", "cache_type": "all"}),
        ]
        assert detect_cycle(actions) is True

    def test_empty_returns_false(self):
        """Empty list has no cycle."""
        assert detect_cycle([]) is False


# ═══════════════════════════════════════════════════════════════════════════════
# Value Safety Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckValueSafety:
    """Tests for _check_value_safety."""

    def test_clean_string_passes(self):
        """A normal string should have no errors."""
        assert _check_value_safety("auth-service") == []

    def test_shell_metachar_detected(self):
        """Shell metacharacters should be caught."""
        errors = _check_value_safety("test; echo pwned")
        assert len(errors) > 0

    def test_url_detected(self):
        """URLs should be flagged."""
        errors = _check_value_safety("https://evil.example.com")
        assert len(errors) > 0

    def test_nested_dict_checked(self):
        """Dangerous values inside nested dicts should be found."""
        errors = _check_value_safety({"inner": {"deep": "http://x.com"}})
        assert len(errors) > 0

    def test_list_items_checked(self):
        """Dangerous values inside lists should be found."""
        errors = _check_value_safety(["safe", "../../../etc/passwd"])
        assert len(errors) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# sanitize_arguments Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSanitizeArguments:
    """Tests for sanitize_arguments."""

    def test_clean_args_unchanged(self):
        """Clean arguments should pass through unchanged."""
        args = {"service_name": "auth-service", "replicas": 3}
        result = sanitize_arguments(args)
        assert result == args

    def test_removes_shell_metachar(self):
        """Shell metacharacters should be stripped from string values."""
        args = {"service_name": "auth-service; rm -rf /"}
        result = sanitize_arguments(args)
        assert ";" not in result["service_name"]

    def test_removes_url(self):
        """URLs should be stripped from string values."""
        args = {"target_revision": "https://evil.com/payload"}
        result = sanitize_arguments(args)
        assert "https://" not in result["target_revision"]


# ═══════════════════════════════════════════════════════════════════════════════
# check_action_eligibility Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckActionEligibility:
    """Tests for check_action_eligibility."""

    def test_eligible_no_dependencies(self):
        """Action with no dependencies should be eligible."""
        action = _make_action(action_id="act-1")
        eligible, reason = check_action_eligibility(action, {}, [action])
        assert eligible is True
        assert reason == ""

    def test_already_executed(self):
        """Already-executed action should not be eligible."""
        action = _make_action(action_id="act-1")
        receipts = {"act-1": {"status": "success"}}
        eligible, reason = check_action_eligibility(action, receipts, [action])
        assert eligible is False
        assert "already executed" in reason

    def test_dependency_not_yet_executed(self):
        """Action whose dependency hasn't run should not be eligible."""
        dep = _make_action(action_id="act-dep")
        action = _make_action(action_id="act-main", depends_on=["act-dep"])
        eligible, reason = check_action_eligibility(action, {}, [dep, action])
        assert eligible is False
        assert "not been executed" in reason

    def test_dependency_succeeded(self):
        """Action with a successful dependency should be eligible."""
        dep = _make_action(action_id="act-dep")
        action = _make_action(action_id="act-main", depends_on=["act-dep"])
        receipts = {"act-dep": {"status": "success"}}
        eligible, reason = check_action_eligibility(action, receipts, [dep, action])
        assert eligible is True
