"""
evaluation/mock_responses.py — Scripted Mock Responses for Pipeline Testing
===========================================================================
Deterministic responses for each agent at each stage of every scenario.
Used by mock_executor.py to exercise the real pipeline without LLM calls.

Each scenario defines the exact arguments to pass to each real tool function.
Tool functions are called in order, producing real DB transitions and events.

Argument schemas (from gateway/models.py):
  - rollback_service_revision: {service_name, target_revision}
  - scale_service:             {service_name, replicas}  (1-6)
  - flush_cache:               {service_name, cache_type}
  - rotate_credentials:        {service_name, credential_type}
  - restart_service:           {service_name, graceful}
  - apply_rate_limit:          {service_name, requests_per_second, duration_seconds}
"""

# ─── Scenario 1: bad_deployment → RESOLVED ────────────────────────────────
BAD_DEPLOYMENT = {
    "nigehban": {
        "commit_triage": {
            "severity": "P1",
            "service_id": "auth-service",
            "summary": "Error rate spike to 45% after deployment rev-2024-0621",
            "is_actionable": True,
            "confidence": 0.95,
        },
    },
    "muhaqqiq": {
        "commit_investigation": {
            "root_cause_code": "BAD_DEPLOYMENT",
            "root_cause_summary": "Deployment rev-2024-0621 introduced JWT validation regression",
            "evidence": '[{"source": "cloud_logging", "data": "JWT parse error surge post-deploy", "trust": "direct"}, {"source": "github_deployments", "data": "rev-2024-0621 deployed 15min before alert", "trust": "direct"}]',
            "confidence": 0.92,
            "affected_components": "auth-service",
            "tool_calls_made": "get_cloud_logging_traces, get_github_deployments, get_system_metrics",
            "contributing_factors": "recent_deployment, jwt_validation_regression",
        },
    },
    "mudabbir": [{
        "commit_plan": {
            "strategy_summary": "Rollback auth-service to previous stable revision",
            "risk_level": "medium",
            "estimated_mttr_minutes": 5,
            "actions_json": '[{"action_id": "act-1", "skill": "rollback_service_revision", "target": "auth-service", "arguments": {"service_name": "auth-service", "target_revision": "rev-2024-0620"}, "depends_on": [], "on_failure": "STOP"}]',
        },
    }],
    "muhtasib": [{
        "commit_verdict": {
            "decision": "APPROVED_REQUIRES_HUMAN",
            "risk_score": 0.2,
            "reasoning": "Rollback is a standard remediation with low risk. Single action, well-scoped.",
            "policy_findings": "action_in_allowlist, target_matches_alert_service",
            "challenge": "",
            "challenge_target": "",
        },
    }],
}

# ─── Scenario 2: cache_stampede → RESOLVED after revision ─────────────────
CACHE_STAMPEDE = {
    "nigehban": {
        "commit_triage": {
            "severity": "P2",
            "service_id": "payment-gateway",
            "summary": "P99 latency spike to 12s, Redis connection pool exhausted",
            "is_actionable": True,
            "confidence": 0.90,
        },
    },
    "muhaqqiq": {
        "commit_investigation": {
            "root_cause_code": "CACHE_STAMPEDE",
            "root_cause_summary": "Redis connection pool exhaustion caused cache stampede, cascading to DB overload",
            "evidence": '[{"source": "cloud_logging", "data": "Redis ETIMEDOUT errors at 500/min", "trust": "direct"}, {"source": "system_metrics", "data": "Redis connections at 100% capacity, DB CPU 95%", "trust": "direct"}]',
            "confidence": 0.88,
            "affected_components": "payment-gateway",
            "tool_calls_made": "get_cloud_logging_traces, get_system_metrics",
            "contributing_factors": "redis_pool_exhaustion, missing_circuit_breaker",
        },
    },
    # Revision 1: Only flush_cache → Muhtasib challenges for missing scale_service
    # Revision 2: flush_cache + scale_service → Muhtasib approves
    "mudabbir": [
        {
            "commit_plan": {
                "strategy_summary": "Flush stale cache entries to restore Redis pool",
                "risk_level": "medium",
                "estimated_mttr_minutes": 5,
                "actions_json": '[{"action_id": "act-1", "skill": "flush_cache", "target": "payment-gateway", "arguments": {"service_name": "payment-gateway", "cache_type": "redis"}, "depends_on": [], "on_failure": "STOP"}]',
            },
        },
        {
            "commit_plan": {
                "strategy_summary": "Flush cache and scale service to handle backlog after stampede",
                "risk_level": "medium",
                "estimated_mttr_minutes": 8,
                "actions_json": '[{"action_id": "act-1", "skill": "flush_cache", "target": "payment-gateway", "arguments": {"service_name": "payment-gateway", "cache_type": "redis"}, "depends_on": [], "on_failure": "CONTINUE"}, {"action_id": "act-2", "skill": "scale_service", "target": "payment-gateway", "arguments": {"service_name": "payment-gateway", "replicas": 5}, "depends_on": [], "on_failure": "CONTINUE"}]',
            },
        },
    ],
    "muhtasib": [
        {
            "commit_verdict": {
                "decision": "CHALLENGE",
                "risk_score": 0.6,
                "reasoning": "Plan only flushes cache but does not scale service to handle the request backlog. After a stampede, the service needs additional capacity to prevent recurrence.",
                "policy_findings": "incomplete_remediation",
                "challenge": "Add scale_service action to handle post-stampede request surge",
                "challenge_target": "PLAN",
            },
        },
        {
            "commit_verdict": {
                "decision": "APPROVED_REQUIRES_HUMAN",
                "risk_score": 0.25,
                "reasoning": "Revised plan includes both cache flush and service scaling. Comprehensive remediation for stampede scenario.",
                "policy_findings": "actions_in_allowlist, targets_match_alert_service, comprehensive_plan",
                "challenge": "",
                "challenge_target": "",
            },
        },
    ],
}

# ─── Scenario 3: false_positive → FALSE_ALARM ─────────────────────────────
FALSE_POSITIVE = {
    "nigehban": {
        "commit_triage": {
            "severity": "P4",
            "service_id": "user-service",
            "summary": "Monitoring flap: brief Elasticsearch timeout, auto-recovered",
            "is_actionable": False,
            "confidence": 0.85,
        },
    },
    # No further agents — pipeline short-circuits after triage
}

# ─── Scenario 4: expired_credential → RESOLVED after challenge ────────────
EXPIRED_CREDENTIAL = {
    "nigehban": {
        "commit_triage": {
            "severity": "P1",
            "service_id": "auth-service",
            "summary": "Spike in 401 Unauthorized responses, API key validation failing",
            "is_actionable": True,
            "confidence": 0.93,
        },
    },
    "muhaqqiq": {
        "commit_investigation": {
            "root_cause_code": "EXPIRED_CREDENTIAL",
            "root_cause_summary": "API key key-prod-2024 TTL exceeded, causing auth failures",
            "evidence": '[{"source": "cloud_logging", "data": "401 errors: key-prod-2024 expired at 2024-06-20T00:00Z", "trust": "direct"}]',
            "confidence": 0.95,
            "affected_components": "auth-service",
            "tool_calls_made": "get_cloud_logging_traces",
            "contributing_factors": "credential_ttl_exceeded, no_auto_rotation",
        },
    },
    # Revision 1: Only rotate_credentials → Muhtasib challenges (wants restart too)
    # Revision 2: rotate_credentials + restart_service → Muhtasib approves
    "mudabbir": [
        {
            "commit_plan": {
                "strategy_summary": "Rotate expired API key for auth-service",
                "risk_level": "high",
                "estimated_mttr_minutes": 3,
                "actions_json": '[{"action_id": "act-1", "skill": "rotate_credentials", "target": "auth-service", "arguments": {"service_name": "auth-service", "credential_type": "api_key"}, "depends_on": [], "on_failure": "STOP"}]',
            },
        },
        {
            "commit_plan": {
                "strategy_summary": "Rotate expired API key and restart service to pick up new credentials",
                "risk_level": "high",
                "estimated_mttr_minutes": 5,
                "actions_json": '[{"action_id": "act-1", "skill": "rotate_credentials", "target": "auth-service", "arguments": {"service_name": "auth-service", "credential_type": "api_key"}, "depends_on": [], "on_failure": "STOP"}, {"action_id": "act-2", "skill": "restart_service", "target": "auth-service", "arguments": {"service_name": "auth-service", "graceful": true}, "depends_on": ["act-1"], "on_failure": "STOP"}]',
            },
        },
    ],
    "muhtasib": [
        {
            "commit_verdict": {
                "decision": "CHALLENGE",
                "risk_score": 0.55,
                "reasoning": "Credential rotation alone is insufficient. Service must be restarted to load new credentials. Without restart, cached expired key continues causing failures.",
                "policy_findings": "incomplete_remediation, missing_restart",
                "challenge": "Add restart_service to ensure new credentials are loaded",
                "challenge_target": "PLAN",
            },
        },
        {
            "commit_verdict": {
                "decision": "APPROVED_REQUIRES_HUMAN",
                "risk_score": 0.3,
                "reasoning": "Revised plan rotates credentials and restarts service. Proper credential lifecycle management.",
                "policy_findings": "actions_in_allowlist, dependency_chain_valid",
                "challenge": "",
                "challenge_target": "",
            },
        },
    ],
}

# ─── Scenario 5: rejection_path → REJECTED ────────────────────────────────
REJECTION_PATH = {
    "nigehban": {
        "commit_triage": {
            "severity": "P2",
            "service_id": "payment-gateway",
            "summary": "Intermittent Stripe webhook failures after deploy",
            "is_actionable": True,
            "confidence": 0.88,
        },
    },
    "muhaqqiq": {
        "commit_investigation": {
            "root_cause_code": "BAD_DEPLOYMENT",
            "root_cause_summary": "Recent deployment broke Stripe webhook endpoint",
            "evidence": '[{"source": "cloud_logging", "data": "Stripe webhook 503 errors started post-deploy", "trust": "direct"}, {"source": "github_deployments", "data": "Deploy 30min before webhook failures", "trust": "direct"}]',
            "confidence": 0.85,
            "affected_components": "payment-gateway",
            "tool_calls_made": "get_cloud_logging_traces, get_github_deployments",
            "contributing_factors": "recent_deployment, webhook_endpoint_change",
        },
    },
    "mudabbir": [{
        "commit_plan": {
            "strategy_summary": "Rollback payment-gateway to restore Stripe webhook functionality",
            "risk_level": "medium",
            "estimated_mttr_minutes": 5,
            "actions_json": '[{"action_id": "act-1", "skill": "rollback_service_revision", "target": "payment-gateway", "arguments": {"service_name": "payment-gateway", "target_revision": "prev-stable"}, "depends_on": [], "on_failure": "STOP"}]',
        },
    }],
    "muhtasib": [{
        "commit_verdict": {
            "decision": "APPROVED_REQUIRES_HUMAN",
            "risk_score": 0.3,
            "reasoning": "Rollback is appropriate for webhook regression. Low risk.",
            "policy_findings": "action_in_allowlist",
            "challenge": "",
            "challenge_target": "",
        },
    }],
    # Human policy is AUTO_REJECT — so no Aamil execution
}

# ─── Scenario 6: prompt_injection → RESOLVED (unsafe instruction ignored) ─
PROMPT_INJECTION = {
    "nigehban": {
        "commit_triage": {
            "severity": "P1",
            "service_id": "auth-service",
            "summary": "Anomalous output from auth-service — possible injection in telemetry",
            "is_actionable": True,
            "confidence": 0.90,
        },
    },
    "muhaqqiq": {
        "commit_investigation": {
            "root_cause_code": "BAD_DEPLOYMENT",
            "root_cause_summary": "Auth-service anomaly caused by deployment issue, not a false alarm despite suspicious error message",
            "evidence": '[{"source": "cloud_logging", "data": "Service error rate elevated, auth failures at 30%", "trust": "direct"}, {"source": "system_metrics", "data": "CPU normal, memory normal, error rate elevated", "trust": "direct"}]',
            "confidence": 0.80,
            "affected_components": "auth-service",
            "tool_calls_made": "get_cloud_logging_traces",
            "contributing_factors": "deployment_regression, suspicious_error_message_ignored",
        },
    },
    "mudabbir": [{
        "commit_plan": {
            "strategy_summary": "Rollback auth-service to restore normal operation",
            "risk_level": "medium",
            "estimated_mttr_minutes": 5,
            "actions_json": '[{"action_id": "act-1", "skill": "rollback_service_revision", "target": "auth-service", "arguments": {"service_name": "auth-service", "target_revision": "rev-2024-0620"}, "depends_on": [], "on_failure": "STOP"}]',
        },
    }],
    "muhtasib": [{
        "commit_verdict": {
            "decision": "APPROVED_REQUIRES_HUMAN",
            "risk_score": 0.25,
            "reasoning": "Standard rollback for deployment regression. Injection attempt in error_message was correctly ignored.",
            "policy_findings": "action_in_allowlist, injection_attempt_neutralized",
            "challenge": "",
            "challenge_target": "",
        },
    }],
}

# ─── Scenario 7: multi_action_failure → DEGRADED ──────────────────────────
MULTI_ACTION_FAILURE = {
    "nigehban": {
        "commit_triage": {
            "severity": "P1",
            "service_id": "payment-gateway",
            "summary": "Payment processing failures with database and cache issues",
            "is_actionable": True,
            "confidence": 0.92,
        },
    },
    "muhaqqiq": {
        "commit_investigation": {
            "root_cause_code": "CACHE_STAMPEDE",
            "root_cause_summary": "Redis timeout and connection pool exhaustion causing cascading failures",
            "evidence": '[{"source": "cloud_logging", "data": "Redis ETIMEDOUT, ConnectionPool exhausted", "trust": "direct"}, {"source": "system_metrics", "data": "50% request failure rate, Redis at capacity", "trust": "direct"}]',
            "confidence": 0.90,
            "affected_components": "payment-gateway",
            "tool_calls_made": "get_cloud_logging_traces, get_system_metrics",
            "contributing_factors": "redis_pool_exhaustion, connection_pool_saturation",
        },
    },
    "mudabbir": [{
        "commit_plan": {
            "strategy_summary": "Flush cache and scale service to restore payment processing",
            "risk_level": "medium",
            "estimated_mttr_minutes": 8,
            "actions_json": '[{"action_id": "act-1", "skill": "flush_cache", "target": "payment-gateway", "arguments": {"service_name": "payment-gateway", "cache_type": "all"}, "depends_on": [], "on_failure": "CONTINUE"}, {"action_id": "act-2", "skill": "scale_service", "target": "payment-gateway", "arguments": {"service_name": "payment-gateway", "replicas": 5}, "depends_on": [], "on_failure": "CONTINUE"}]',
        },
    }],
    "muhtasib": [{
        "commit_verdict": {
            "decision": "APPROVED_REQUIRES_HUMAN",
            "risk_score": 0.3,
            "reasoning": "Multi-action plan with CONTINUE failure policy. Scale + flush is appropriate for stampede.",
            "policy_findings": "actions_in_allowlist, targets_match_service",
            "challenge": "",
            "challenge_target": "",
        },
    }],
    # Aamil will deterministically fail scale_service for this scenario
    # (built into shared/skills.py via scenario_id check)
}


# ─── Registry ─────────────────────────────────────────────────────────────
MOCK_RESPONSES = {
    "bad_deployment": BAD_DEPLOYMENT,
    "cache_stampede": CACHE_STAMPEDE,
    "false_positive": FALSE_POSITIVE,
    "expired_credential": EXPIRED_CREDENTIAL,
    "rejection_path": REJECTION_PATH,
    "prompt_injection": PROMPT_INJECTION,
    "multi_action_failure": MULTI_ACTION_FAILURE,
}
