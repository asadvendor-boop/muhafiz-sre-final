"""
shared/action_policy.py – Deterministic Action Validation (§16)
=====================================================================

All action envelopes pass through deterministic validation before
contract issuance, before approval, and before execution.

No shell commands. No URLs. No injection. Typed, bounded, acyclic.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

from gateway.models import (
    ActionEnvelope,
    AllowedSkill,
    FailurePolicy,
    SKILL_ARGUMENT_SCHEMAS,
)

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

ALLOWED_SKILLS = frozenset(s.value for s in AllowedSkill)

FORBIDDEN_OPERATIONS = frozenset({
    "delete_database",
    "drop_table",
    "exfiltrate_secret",
    "rotate_all_credentials",
    "disable_audit",
    "rm_rf",
    "format_disk",
    "shutdown_cluster",
})

ALLOWED_TARGETS = frozenset({
    "auth-service",
    "payment-gateway",
    "user-service",
})

# Dangerous patterns in argument values
_SHELL_METACHAR_RE = re.compile(r"[|;&$`\n\r]")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_PATH_TRAVERSAL_RE = re.compile(r"\.\./")
_CMD_INJECTION_RE = re.compile(
    r"(?:&&|\|\||;|`|\$\(|>\s|<\s|>>|<<)", re.IGNORECASE
)


# ────────────────────────────────────────────────────────────────────────────
# Argument sanitisation
# ────────────────────────────────────────────────────────────────────────────

def _check_value_safety(value: Any, path: str = "") -> list[str]:
    """
    Recursively check a value for dangerous patterns.

    Returns a list of error messages for any violations found.
    """
    errors: list[str] = []
    if isinstance(value, str):
        if _SHELL_METACHAR_RE.search(value):
            errors.append(
                f"Shell metacharacter in argument {path!r}: {value!r}"
            )
        if _URL_RE.search(value):
            errors.append(f"URL in argument {path!r}: {value!r}")
        if _PATH_TRAVERSAL_RE.search(value):
            errors.append(f"Path traversal in argument {path!r}: {value!r}")
        if _CMD_INJECTION_RE.search(value):
            errors.append(
                f"Command injection pattern in argument {path!r}: {value!r}"
            )
    elif isinstance(value, dict):
        for k, v in value.items():
            errors.extend(_check_value_safety(v, f"{path}.{k}"))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            errors.extend(_check_value_safety(v, f"{path}[{i}]"))
    return errors


def sanitize_arguments(arguments: dict) -> dict:
    """
    Return a sanitized copy of arguments with dangerous patterns removed.

    This is a defence-in-depth measure. The primary defence is typed
    argument validation via Pydantic schemas.
    """
    cleaned = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            value = _SHELL_METACHAR_RE.sub("", value)
            value = _URL_RE.sub("", value)
            value = _PATH_TRAVERSAL_RE.sub("", value)
        cleaned[key] = value
    return cleaned


# ────────────────────────────────────────────────────────────────────────────
# Single-action validation
# ────────────────────────────────────────────────────────────────────────────

def validate_single_action(
    action: ActionEnvelope,
) -> tuple[bool, list[str]]:
    """
    Validate one action envelope in isolation.

    Checks:
        - Skill is allowed
        - Skill is not forbidden
        - Target is in allowlist
        - Arguments pass typed schema validation
        - No dangerous patterns in argument values

    Returns:
        (True, []) on success, (False, errors) on failure.
    """
    errors: list[str] = []

    # Skill allowlist
    if action.skill.value not in ALLOWED_SKILLS:
        errors.append(f"Skill {action.skill.value!r} is not allowed.")

    # Forbidden operation check
    if action.skill.value in FORBIDDEN_OPERATIONS:
        errors.append(
            f"Skill {action.skill.value!r} is a forbidden operation."
        )

    # Target allowlist
    if action.target not in ALLOWED_TARGETS:
        errors.append(
            f"Target {action.target!r} is not in the allowed target list: "
            f"{sorted(ALLOWED_TARGETS)}"
        )

    # Self-dependency
    if action.action_id in action.depends_on:
        errors.append(
            f"Action {action.action_id!r} depends on itself."
        )

    # Typed argument validation
    schema_cls = SKILL_ARGUMENT_SCHEMAS.get(action.skill)
    if schema_cls:
        try:
            schema_cls(**action.arguments)
        except Exception as exc:
            errors.append(
                f"Action {action.action_id!r} argument validation failed: "
                f"{exc}"
            )

    # Argument safety check
    arg_errors = _check_value_safety(action.arguments, action.action_id)
    errors.extend(arg_errors)

    return (len(errors) == 0, errors)


# ────────────────────────────────────────────────────────────────────────────
# Graph validation
# ────────────────────────────────────────────────────────────────────────────

def detect_cycle(actions: list[ActionEnvelope]) -> bool:
    """
    Detect cycles in the action dependency graph using Kahn's algorithm.

    Returns True if a cycle exists.
    """
    if not actions:
        return False

    id_set = {a.action_id for a in actions}
    in_degree: dict[str, int] = defaultdict(int)
    adjacency: dict[str, list[str]] = defaultdict(list)

    for action in actions:
        if action.action_id not in in_degree:
            in_degree[action.action_id] = 0
        for dep in action.depends_on:
            if dep in id_set:
                adjacency[dep].append(action.action_id)
                in_degree[action.action_id] += 1

    queue = deque(
        aid for aid, deg in in_degree.items() if deg == 0
    )
    visited = 0

    while queue:
        node = queue.popleft()
        visited += 1
        for neighbour in adjacency[node]:
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    return visited != len(id_set)


def topological_sort(
    actions: list[ActionEnvelope],
) -> list[ActionEnvelope]:
    """
    Return actions in dependency order (topological sort).

    Raises ValueError if a cycle is detected.
    """
    if not actions:
        return []

    action_map = {a.action_id: a for a in actions}
    in_degree: dict[str, int] = defaultdict(int)
    adjacency: dict[str, list[str]] = defaultdict(list)

    for action in actions:
        if action.action_id not in in_degree:
            in_degree[action.action_id] = 0
        for dep in action.depends_on:
            if dep in action_map:
                adjacency[dep].append(action.action_id)
                in_degree[action.action_id] += 1

    queue = deque(
        aid for aid, deg in in_degree.items() if deg == 0
    )
    result: list[ActionEnvelope] = []

    while queue:
        node = queue.popleft()
        result.append(action_map[node])
        for neighbour in adjacency[node]:
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    if len(result) != len(actions):
        raise ValueError("Cycle detected in action dependency graph.")

    return result


def validate_action_graph(
    actions: list[ActionEnvelope],
) -> tuple[bool, list[str]]:
    """
    Validate the complete action graph (§16.4).

    Checks:
        - Unique action identifiers
        - Allowed skill names
        - Typed arguments
        - Target allowlists
        - All dependencies exist
        - Acyclic dependency graph
        - No self-dependencies
        - Bounded numeric parameters
        - No dangerous patterns in arguments

    Returns:
        (True, []) on success, (False, errors) on failure.
    """
    errors: list[str] = []

    if not actions:
        return (True, [])

    # Unique action IDs
    ids = [a.action_id for a in actions]
    if len(ids) != len(set(ids)):
        seen = set()
        for aid in ids:
            if aid in seen:
                errors.append(f"Duplicate action_id: {aid!r}")
            seen.add(aid)

    id_set = set(ids)

    # Validate each action individually
    for action in actions:
        ok, action_errors = validate_single_action(action)
        errors.extend(action_errors)

        # Check dependencies exist
        for dep in action.depends_on:
            if dep not in id_set:
                errors.append(
                    f"Action {action.action_id!r} depends on "
                    f"non-existent action {dep!r}."
                )

    # Cycle detection
    if detect_cycle(actions):
        errors.append("Dependency graph contains a cycle.")

    return (len(errors) == 0, errors)


# ────────────────────────────────────────────────────────────────────────────
# Execution eligibility
# ────────────────────────────────────────────────────────────────────────────

def check_action_eligibility(
    action: ActionEnvelope,
    executed_receipts: dict[str, dict],
    all_actions: list[ActionEnvelope],
) -> tuple[bool, str]:
    """
    Check if an action is eligible to execute (§16.5).

    An action is eligible when:
        - It hasn't already been executed
        - All dependencies have successful receipts
        - For failed dependencies, check on_failure policy

    Args:
        action: The action to check.
        executed_receipts: Map of action_id → receipt dict.
        all_actions: All actions in the plan (for policy lookup).

    Returns:
        (True, '') if eligible, (False, reason) if not.
    """
    # Already executed
    if action.action_id in executed_receipts:
        return (False, f"Action {action.action_id!r} already executed.")

    # Check dependencies
    action_map = {a.action_id: a for a in all_actions}

    for dep_id in action.depends_on:
        if dep_id not in executed_receipts:
            return (
                False,
                f"Dependency {dep_id!r} has not been executed yet.",
            )

        receipt = executed_receipts[dep_id]
        if receipt.get("status") != "success":
            # Check the dependency's on_failure policy
            dep_action = action_map.get(dep_id)
            if dep_action and dep_action.on_failure == FailurePolicy.STOP:
                return (
                    False,
                    f"Dependency {dep_id!r} failed with STOP policy.",
                )

    return (True, "")
