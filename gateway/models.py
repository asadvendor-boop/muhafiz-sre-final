"""
gateway/models.py – Pydantic v2 Domain Models for MuhafizSRE
===================================================================

Every data shape that crosses the gateway boundary is defined here.
Canonical serialisation is deterministic for hash-chain integrity.

invariants enforced:
    - Typed action envelopes with per-skill argument schemas
    - Bounded numeric parameters
    - Deterministic canonical_json for hash stability
    - Strict enum vocabularies for all state machines
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ────────────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    """
    Deterministic JSON serialisation for hash stability.

    Rules (§11):
        sort_keys=True, separators=(',',':'),
        ensure_ascii=False, allow_nan=False
    """
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def sha256_hex(value: Any) -> str:
    """SHA-256 hex digest of canonical_json(value)."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────────────
# Enums – constrained vocabularies for all state machines
# ────────────────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    """PagerDuty-style severity tiers."""
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class IncidentStatus(str, Enum):
    """Full incident lifecycle states (§9)."""
    DETECTED = "DETECTED"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    REVIEWING = "REVIEWING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    FALSE_ALARM = "FALSE_ALARM"
    BLOCKED = "BLOCKED"
    ESCALATED = "ESCALATED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    PIPELINE_FAILED = "PIPELINE_FAILED"
    DEGRADED = "DEGRADED"


class ContractStatus(str, Enum):
    """Approval contract lifecycle (§10.4)."""
    ISSUED = "ISSUED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    CONSUMED = "CONSUMED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class RunStatus(str, Enum):
    """Pipeline run status."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SafetyDecision(str, Enum):
    """Muhtasib verdict categories (§13.6)."""
    APPROVED_REQUIRES_HUMAN = "APPROVED_REQUIRES_HUMAN"
    CHALLENGE = "CHALLENGE"
    BLOCKED_UNSAFE = "BLOCKED_UNSAFE"
    ESCALATE = "ESCALATE"


class ChallengeTarget(str, Enum):
    """What the safety challenge targets (§13.6)."""
    EVIDENCE = "EVIDENCE"
    PLAN = "PLAN"


class RootCauseCode(str, Enum):
    """Deterministic root-cause classification (§8)."""
    BAD_DEPLOYMENT = "BAD_DEPLOYMENT"
    SCHEMA_MIGRATION = "SCHEMA_MIGRATION"
    CACHE_STAMPEDE = "CACHE_STAMPEDE"
    EXPIRED_CREDENTIAL = "EXPIRED_CREDENTIAL"
    TELEMETRY_FAILURE = "TELEMETRY_FAILURE"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    UNKNOWN = "UNKNOWN"


class AllowedSkill(str, Enum):
    """Allowlisted remediation skills (§16)."""
    ROLLBACK_SERVICE_REVISION = "rollback_service_revision"
    APPLY_RATE_LIMIT = "apply_rate_limit"
    SCALE_SERVICE = "scale_service"
    FLUSH_CACHE = "flush_cache"
    ROTATE_CREDENTIALS = "rotate_credentials"
    RESTART_SERVICE = "restart_service"


class AllowedService(str, Enum):
    """Allowlisted target services."""
    AUTH_SERVICE = "auth-service"
    PAYMENT_GATEWAY = "payment-gateway"
    USER_SERVICE = "user-service"


class FailurePolicy(str, Enum):
    """Action failure handling policy (§16.5)."""
    STOP = "STOP"
    CONTINUE = "CONTINUE"


class DecisionAction(str, Enum):
    """Human decision actions (§17.3)."""
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_REVISION = "REQUEST_REVISION"
    MARK_FALSE_ALARM = "MARK_FALSE_ALARM"


class HumanPolicy(str, Enum):
    """Deterministic human policy for evaluation (§23.3)."""
    AUTO_APPROVE = "AUTO_APPROVE"
    AUTO_REJECT = "AUTO_REJECT"
    AUTO_REVISE = "AUTO_REVISE"


class RecoveryOracle(str, Enum):
    """Recovery verification oracle type (§23.3)."""
    VICTIM_HEALTH = "VICTIM_HEALTH"
    SCENARIO_STATE = "SCENARIO_STATE"
    NONE = "NONE"


class RiskLevel(str, Enum):
    """Risk classification for mitigation plans."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ────────────────────────────────────────────────────────────────────────────
# Core domain models
# ────────────────────────────────────────────────────────────────────────────

class Alert(BaseModel):
    """Raw incident alert payload (§8)."""
    severity: Severity = Field(
        ..., description="Incident severity tier (P0 = most critical)."
    )
    service_id: str = Field(
        ..., min_length=1, max_length=128,
        description="Affected service identifier.",
    )
    summary: str = Field(
        ..., min_length=1, max_length=1024,
        description="Human-readable incident summary.",
    )
    alert_type: str = Field(
        default="generic",
        description="Alert category (e.g., latency, error_rate, deployment).",
    )
    error_message: str = Field(
        default="",
        description="Error message from the alerting system.",
    )
    timestamp: str = Field(
        default_factory=_utc_now,
        description="ISO-8601 UTC timestamp of detection.",
    )

    @field_validator("service_id")
    @classmethod
    def _sanitise_service_id(cls, v: str) -> str:
        """Reject IDs with disallowed characters."""
        cleaned = v.strip()
        if not re.match(r"^[a-zA-Z0-9._-]+$", cleaned):
            raise ValueError(
                f"service_id contains disallowed characters: {cleaned!r}. "
                "Only alphanumeric, hyphens, underscores, and dots are permitted."
            )
        return cleaned


# ────────────────────────────────────────────────────────────────────────────
# Per-skill argument models (§16.3)
# ────────────────────────────────────────────────────────────────────────────

class RollbackArguments(BaseModel):
    """Arguments for rollback_service_revision skill."""
    service_name: AllowedService = Field(
        ..., description="Target service for rollback."
    )
    target_revision: str = Field(
        ..., pattern=r"^[A-Za-z0-9._-]{1,64}$",
        description="Revision identifier to roll back to.",
    )


class ScaleArguments(BaseModel):
    """Arguments for scale_service skill."""
    service_name: AllowedService = Field(
        ..., description="Target service to scale."
    )
    replicas: int = Field(
        ..., ge=1, le=6,
        description="Target replica count (bounded 1-6).",
    )


class RateLimitArguments(BaseModel):
    """Arguments for apply_rate_limit skill."""
    service_name: AllowedService = Field(
        ..., description="Target service for rate limiting."
    )
    requests_per_second: int = Field(
        ..., ge=1, le=1000,
        description="Rate limit in requests per second.",
    )
    duration_seconds: int = Field(
        ..., ge=30, le=900,
        description="Duration of rate limit in seconds.",
    )


class FlushCacheArguments(BaseModel):
    """Arguments for flush_cache skill."""
    service_name: AllowedService = Field(
        ..., description="Target service whose cache to flush."
    )
    cache_type: str = Field(
        default="all", max_length=64,
        description="Cache type to flush (e.g., 'redis', 'jwks', 'all').",
    )


class RotateCredentialsArguments(BaseModel):
    """Arguments for rotate_credentials skill."""
    service_name: AllowedService = Field(
        ..., description="Target service for credential rotation."
    )
    credential_type: str = Field(
        ..., max_length=64,
        description="Credential type (e.g., 'api_key', 'db_password').",
    )


class RestartArguments(BaseModel):
    """Arguments for restart_service skill."""
    service_name: AllowedService = Field(
        ..., description="Target service to restart."
    )
    graceful: bool = Field(
        default=True,
        description="Whether to perform a graceful restart.",
    )


# Mapping from skill enum to argument schema
SKILL_ARGUMENT_SCHEMAS: dict[AllowedSkill, type[BaseModel]] = {
    AllowedSkill.ROLLBACK_SERVICE_REVISION: RollbackArguments,
    AllowedSkill.APPLY_RATE_LIMIT: RateLimitArguments,
    AllowedSkill.SCALE_SERVICE: ScaleArguments,
    AllowedSkill.FLUSH_CACHE: FlushCacheArguments,
    AllowedSkill.ROTATE_CREDENTIALS: RotateCredentialsArguments,
    AllowedSkill.RESTART_SERVICE: RestartArguments,
}


# ────────────────────────────────────────────────────────────────────────────
# Action envelope (§16.2)
# ────────────────────────────────────────────────────────────────────────────

class ActionEnvelope(BaseModel):
    """A single bounded remediation action in the plan."""
    action_id: str = Field(
        ..., min_length=1, max_length=64,
        description="Unique action identifier within the plan.",
    )
    skill: AllowedSkill = Field(
        ..., description="Remediation skill to invoke.",
    )
    target: str = Field(
        ..., min_length=1, max_length=128,
        description="Target resource identifier.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Typed arguments for the skill.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Action IDs this action depends on.",
    )
    on_failure: FailurePolicy = Field(
        default=FailurePolicy.STOP,
        description="Failure handling policy.",
    )


# ────────────────────────────────────────────────────────────────────────────
# Agent output models
# ────────────────────────────────────────────────────────────────────────────

class TriageResult(BaseModel):
    """Nigehban's triage output (§13.3)."""
    severity: Severity = Field(
        ..., description="Assessed severity."
    )
    service_id: str = Field(
        ..., description="Affected service identifier."
    )
    summary: str = Field(
        ..., description="Triage summary."
    )
    is_actionable: bool = Field(
        ..., description="Whether the alert requires action."
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Triage confidence score.",
    )
    handoff_reason: Optional[str] = Field(
        default=None,
        description="Reason for non-actionable classification.",
    )


class InvestigationResult(BaseModel):
    """Muhaqqiq's investigation output (§13.4)."""
    root_cause_code: RootCauseCode = Field(
        ..., description="Deterministic root-cause classification."
    )
    root_cause_summary: str = Field(
        ..., min_length=1,
        description="Human-readable root-cause explanation.",
    )
    evidence: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Evidence items with source, data, and trust classification.",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Investigation confidence score.",
    )
    contributing_factors: list[str] = Field(
        default_factory=list,
        description="Contributing factors identified.",
    )
    affected_components: list[str] = Field(
        default_factory=list,
        description="Components affected by the incident.",
    )
    tool_calls_made: list[str] = Field(
        default_factory=list,
        description="MCP tools invoked during investigation.",
    )


class MitigationPlan(BaseModel):
    """Mudabbir's remediation plan (§13.5)."""
    plan_id: str = Field(
        default_factory=lambda: f"PLAN-{uuid.uuid4().hex[:8].upper()}",
        description="Unique plan identifier.",
    )
    revision: int = Field(
        default=1, ge=1,
        description="Plan revision number.",
    )
    actions: list[ActionEnvelope] = Field(
        default_factory=list,
        description="Ordered list of remediation actions.",
    )
    strategy_summary: str = Field(
        ..., min_length=1,
        description="High-level strategy description.",
    )
    risk_level: RiskLevel = Field(
        ..., description="Risk classification.",
    )
    estimated_mttr_minutes: int = Field(
        default=0, ge=0,
        description="Estimated Mean Time To Repair in minutes.",
    )


class SafetyVerdict(BaseModel):
    """Muhtasib's safety review output (§13.6)."""
    decision: SafetyDecision = Field(
        ..., description="Safety decision category.",
    )
    challenge_target: Optional[ChallengeTarget] = Field(
        default=None,
        description="What the challenge targets (if decision is CHALLENGE).",
    )
    challenge: Optional[str] = Field(
        default=None,
        description="Specific challenge or objection text.",
    )
    risk_score: float = Field(
        ..., ge=0.0, le=1.0,
        description="Quantified risk score.",
    )
    policy_findings: list[str] = Field(
        default_factory=list,
        description="Safety policy findings.",
    )
    reasoning: str = Field(
        ..., min_length=1,
        description="Detailed safety reasoning.",
    )
    first_pass_commit: bool = Field(
        default=True,
        description="True if verdict was committed on the first invocation.",
    )
    retry_used: bool = Field(
        default=False,
        description="True if a bounded retry was needed to obtain this verdict.",
    )


class SkillResult(BaseModel):
    """Result from executing a remediation skill (§20.4)."""
    status: Literal["success", "error"] = Field(
        ..., description="Execution outcome.",
    )
    execution_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex[:8],
        description="Unique execution trace ID.",
    )
    timestamp: str = Field(
        default_factory=_utc_now,
        description="Execution timestamp.",
    )
    service: str = Field(
        default="", description="Target service name.",
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Execution details.",
    )
    adapter: str = Field(
        default="simulated",
        description="Adapter used (simulated or live).",
    )


# ────────────────────────────────────────────────────────────────────────────
# API request/response models
# ────────────────────────────────────────────────────────────────────────────

class CreateIncidentRequest(BaseModel):
    """POST /api/incidents request body."""
    alert: Alert = Field(
        ..., description="Alert payload triggering the incident.",
    )
    scenario_id: Optional[str] = Field(
        default=None, max_length=128,
        description="Evaluation scenario identifier.",
    )


class CreateIncidentResponse(BaseModel):
    """POST /api/incidents response body."""
    incident_id: str = Field(
        ..., description="Assigned incident identifier.",
    )
    run_id: str = Field(
        ..., description="Phase 1 pipeline run identifier.",
    )
    status: IncidentStatus = Field(
        ..., description="Initial incident status.",
    )
    events_url: str = Field(
        ..., description="SSE events endpoint URL.",
    )


class DecisionRequest(BaseModel):
    """POST /api/incidents/{id}/decisions request body (§17.3)."""
    contract_id: str = Field(
        ..., description="Contract being acted on.",
    )
    revision: int = Field(
        ..., ge=1,
        description="Contract revision number.",
    )
    action: DecisionAction = Field(
        ..., description="Human decision action.",
    )
    approval_token: Optional[str] = Field(
        default=None,
        description="HMAC approval token (required for APPROVE).",
    )
    feedback: Optional[str] = Field(
        default=None, max_length=2048,
        description="Operator feedback or revision instructions.",
    )
    operator_label: str = Field(
        default="Demo Operator",
        description="Display label for the operator.",
    )


# ────────────────────────────────────────────────────────────────────────────
# Workflow models
# ────────────────────────────────────────────────────────────────────────────

class Phase1Input(BaseModel):
    """Input for Phase 1 ADK workflow (§19)."""
    alert: Alert = Field(
        ..., description="Alert that triggered the incident.",
    )
    start_stage: Literal["triage", "investigation", "planning"] = Field(
        default="triage",
        description="Stage to begin processing from.",
    )
    revision_number: int = Field(
        default=1, ge=1,
        description="Plan revision number.",
    )
    revision_feedback: Optional[str] = Field(
        default=None,
        description="Human feedback for revision.",
    )
    prior_triage: Optional[TriageResult] = Field(
        default=None,
        description="Prior triage result (for revision restarts).",
    )
    prior_investigation: Optional[InvestigationResult] = Field(
        default=None,
        description="Prior investigation result (for revision restarts).",
    )


class Phase1Result(BaseModel):
    """Output of Phase 1 ADK workflow."""
    terminal_status: IncidentStatus = Field(
        ..., description="Final status after Phase 1.",
    )
    triage: Optional[TriageResult] = Field(
        default=None, description="Triage output.",
    )
    investigation: Optional[InvestigationResult] = Field(
        default=None, description="Investigation output.",
    )
    plan: Optional[MitigationPlan] = Field(
        default=None, description="Mitigation plan output.",
    )
    verdict: Optional[SafetyVerdict] = Field(
        default=None, description="Safety verdict output.",
    )

    @classmethod
    def false_alarm(cls, triage: TriageResult) -> "Phase1Result":
        """Construct a false-alarm result."""
        return cls(
            terminal_status=IncidentStatus.FALSE_ALARM,
            triage=triage,
        )

    @classmethod
    def blocked(cls, triage: TriageResult, verdict: SafetyVerdict) -> "Phase1Result":
        """Construct a blocked-unsafe result."""
        return cls(
            terminal_status=IncidentStatus.BLOCKED,
            triage=triage,
            verdict=verdict,
        )

    @classmethod
    def escalated(
        cls,
        triage: Optional[TriageResult] = None,
        investigation: Optional[InvestigationResult] = None,
        reason: str = "",
    ) -> "Phase1Result":
        """Construct an escalated result."""
        return cls(
            terminal_status=IncidentStatus.ESCALATED,
            triage=triage,
            investigation=investigation,
        )

    @classmethod
    def awaiting_approval(
        cls,
        triage: TriageResult,
        investigation: InvestigationResult,
        plan: MitigationPlan,
        verdict: SafetyVerdict,
    ) -> "Phase1Result":
        """Construct a result awaiting human approval."""
        return cls(
            terminal_status=IncidentStatus.AWAITING_APPROVAL,
            triage=triage,
            investigation=investigation,
            plan=plan,
            verdict=verdict,
        )

    @classmethod
    def escalated_for_missing_evidence(
        cls, triage: TriageResult
    ) -> "Phase1Result":
        """Escalate due to insufficient evidence."""
        return cls(
            terminal_status=IncidentStatus.ESCALATED,
            triage=triage,
        )

    @classmethod
    def escalated_for_review_limit(
        cls,
        triage: TriageResult,
        investigation: Optional[InvestigationResult] = None,
    ) -> "Phase1Result":
        """Escalate due to exceeding review loop limit."""
        return cls(
            terminal_status=IncidentStatus.ESCALATED,
            triage=triage,
            investigation=investigation,
        )

    @classmethod
    def escalated_for_invalid_verdict(
        cls,
        triage: TriageResult,
        investigation: Optional[InvestigationResult] = None,
    ) -> "Phase1Result":
        """Escalate due to invalid safety verdict."""
        return cls(
            terminal_status=IncidentStatus.ESCALATED,
            triage=triage,
            investigation=investigation,
        )


# ────────────────────────────────────────────────────────────────────────────
# Evaluation models (§23)
# ────────────────────────────────────────────────────────────────────────────

class EvaluationScenario(BaseModel):
    """Evaluation scenario definition (§23.3)."""
    id: str = Field(..., description="Scenario identifier.")
    alert: Alert = Field(..., description="Alert for the scenario.")
    expected_terminal_state: IncidentStatus = Field(
        ..., description="Expected final incident state."
    )
    acceptable_terminal_states: set[IncidentStatus] = Field(
        default_factory=set,
        description=(
            "Additional acceptable terminal states beyond expected_terminal_state. "
            "If non-empty, the terminal_state check passes if actual status is in "
            "this set OR equals expected_terminal_state."
        ),
    )
    acceptable_root_cause_codes: set[RootCauseCode] = Field(
        default_factory=set,
        description="Acceptable root-cause classifications.",
    )
    required_tools: set[str] = Field(
        default_factory=set,
        description="Tools that must be used.",
    )
    forbidden_tools: set[str] = Field(
        default_factory=set,
        description="Tools that must not be used.",
    )
    required_actions: set[AllowedSkill] = Field(
        default_factory=set,
        description="Actions that must be in the plan.",
    )
    allowed_actions: set[AllowedSkill] = Field(
        default_factory=set,
        description="Actions that are allowed.",
    )
    forbidden_actions: set[str] = Field(
        default_factory=set,
        description="Actions that are forbidden for this scenario.",
    )
    action_expected: bool = Field(
        default=True,
        description="Whether any remediation action is expected.",
    )
    recovery_applies: bool = Field(
        default=True,
        description="Whether recovery verification applies.",
    )
    challenge_required: bool = Field(
        default=False,
        description="Whether a safety challenge is expected.",
    )
    expected_challenge_target: Optional[ChallengeTarget] = Field(
        default=None,
        description="Expected challenge target if challenge_required.",
    )
    minimum_plan_revision: int = Field(
        default=1, ge=1,
        description="Minimum expected plan revision number.",
    )
    human_policy: Optional[HumanPolicy] = Field(
        default=None,
        description="Deterministic human policy for evaluation.",
    )
    recovery_oracle: RecoveryOracle = Field(
        default=RecoveryOracle.NONE,
        description="Recovery verification oracle type.",
    )
    scenario_id: str = Field(
        ..., description="MCP scenario data identifier.",
    )


# ────────────────────────────────────────────────────────────────────────────
# Room message model (Live agent discussion)
# ────────────────────────────────────────────────────────────────────────────

class RoomMessage(BaseModel):
    """A message in the Live agent discussion room."""
    message_id: str
    incident_id: str
    sequence: int
    sender: str
    sender_display: str
    sender_emoji: str
    sender_color: str
    mentions: list[str] = []
    reply_to: str | None = None
    message_type: str  # triage, investigation, plan, challenge, verdict, execution, system
    content: str
    evidence_ref: str | None = None
    timestamp: str
    message_hash: str
