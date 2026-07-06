"""test_mcp_fixtures.py — Assert MCP overlay data per scenario before live run.

Directly calls the MCP server's data-resolution functions for each scenario
and asserts the returned fixtures contain the intended evidence signals.
This runs WITHOUT spawning an MCP subprocess or calling Gemini.
"""
import json
import os
import sys

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from shared.mcp_server.server import (
    _get_cloud_logging_data,
    _get_metrics_data,
    _get_deployments_data,
    _get_scenario_id,
)


def _set_scenario(scenario_id: str):
    os.environ["MUHAFIZ_SCENARIO_ID"] = scenario_id


def _resolve_lambdas(obj):
    """Recursively resolve lambda values in fixture data."""
    if callable(obj):
        return obj()
    elif isinstance(obj, dict):
        return {k: _resolve_lambdas(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_lambdas(v) for v in obj]
    return obj


def test_cache_stampede():
    """cache_stampede on payment-gateway: Redis/cache evidence, no recent deploy."""
    _set_scenario("cache_stampede")
    
    # Cloud Logging
    logs = _resolve_lambdas(_get_cloud_logging_data("payment-gateway"))
    assert logs is not None, "No cloud logging data for payment-gateway"
    entries_text = " ".join(e.get("text_payload", "") for e in logs.get("entries", []))
    assert "redis" in entries_text.lower() or "cache" in entries_text.lower(), \
        f"Cache/Redis evidence missing from logs: {entries_text[:200]}"
    assert "stampede" in entries_text.lower() or "thundering herd" in entries_text.lower(), \
        f"Stampede evidence missing from logs: {entries_text[:200]}"
    
    # Metrics
    metrics = _resolve_lambdas(_get_metrics_data("payment-gateway"))
    assert metrics is not None, "No metrics for payment-gateway"
    health = metrics.get("health_status") or metrics.get("health", {}).get("status", "")
    assert health in ("CRITICAL", "WARNING"), f"Expected CRITICAL/WARNING health, got: {health}"
    p99 = metrics.get("latency_percentiles", {}).get("p99_ms", {})
    if isinstance(p99, dict):
        assert p99.get("spike_peak", 0) >= 10000, f"P99 spike should be >=10000ms, got {p99}"
    
    # Deployments
    deploys = _resolve_lambdas(_get_deployments_data("payment-gateway"))
    assert deploys is not None, "No deployment data for payment-gateway"
    assert len(deploys) >= 1, "Expected at least 1 deployment"
    latest = deploys[0]
    assert latest.get("status") in ("deployed", "success"), \
        f"Expected stable deployment, got status={latest.get('status')}"
    
    print("  ✅ cache_stampede: Redis/cache logs, saturated metrics, old stable deploy")


def test_expired_credential():
    """expired_credential on auth-service: JWT/key expiry evidence, stable infra."""
    _set_scenario("expired_credential")
    
    # Cloud Logging
    logs = _resolve_lambdas(_get_cloud_logging_data("auth-service"))
    assert logs is not None, "No cloud logging data for auth-service"
    entries_text = " ".join(e.get("text_payload", "") for e in logs.get("entries", []))
    entries_lower = entries_text.lower()
    assert any(k in entries_lower for k in ("expired", "ttl", "key-prod", "jwks", "stale")), \
        f"Credential expiry evidence missing from logs: {entries_text[:300]}"
    assert "expired_credential" not in entries_lower, \
        "Label leakage! 'EXPIRED_CREDENTIAL' found in telemetry text"
    
    # Metrics
    metrics = _resolve_lambdas(_get_metrics_data("auth-service"))
    assert metrics is not None, "No metrics for auth-service"
    # Error rate should spike to show 401s
    err_cfg = metrics.get("metrics_config", {}).get("error_rate", {})
    if err_cfg:
        assert err_cfg.get("spike_peak", 0) >= 50, \
            f"Error rate spike should be >=50%, got {err_cfg}"
    # CPU should be stable (not a resource issue)
    cpu_cfg = metrics.get("metrics_config", {}).get("cpu_utilization", {})
    if cpu_cfg:
        assert cpu_cfg.get("spike_peak", 100) <= 40, \
            f"CPU should be stable (<40%), got {cpu_cfg}"
    
    # Deployments
    deploys = _resolve_lambdas(_get_deployments_data("auth-service"))
    assert deploys is not None, "No deployment data for auth-service"
    latest = deploys[0]
    assert latest.get("status") in ("deployed", "success"), \
        f"Expected stable old deployment, got status={latest.get('status')}"
    
    print("  ✅ expired_credential: JWT/key expiry logs, 401 spike, stable infra, old deploy")


def test_multi_action_failure():
    """multi_action_failure on payment-gateway: same cache evidence as cache_stampede."""
    _set_scenario("multi_action_failure")
    
    logs = _resolve_lambdas(_get_cloud_logging_data("payment-gateway"))
    assert logs is not None, "No cloud logging data for payment-gateway"
    entries_text = " ".join(e.get("text_payload", "") for e in logs.get("entries", []))
    assert "cache" in entries_text.lower() or "redis" in entries_text.lower(), \
        f"Cache evidence missing for multi_action_failure"
    
    metrics = _resolve_lambdas(_get_metrics_data("payment-gateway"))
    assert metrics is not None, "No metrics for payment-gateway"
    
    deploys = _resolve_lambdas(_get_deployments_data("payment-gateway"))
    assert deploys is not None, "No deployments for payment-gateway"
    
    print("  ✅ multi_action_failure: cache evidence (shared with cache_stampede)")


def test_bad_deployment_unchanged():
    """bad_deployment on payment-gateway: should use DEFAULT data (no overlay)."""
    _set_scenario("bad_deployment")
    
    # Should get default data (no overlay for bad_deployment)
    logs = _resolve_lambdas(_get_cloud_logging_data("payment-gateway"))
    assert logs is not None, "No cloud logging for payment-gateway (default)"
    entries_text = " ".join(e.get("text_payload", "") for e in logs.get("entries", []))
    # Default payment-gateway has Stripe webhook / deployment data
    assert "stripe" in entries_text.lower() or "webhook" in entries_text.lower() or "deployment" in entries_text.lower() or "connection" in entries_text.lower(), \
        f"Default payment-gateway data should contain deployment-related info"
    
    print("  ✅ bad_deployment: uses default data (no overlay), deployment evidence present")


if __name__ == "__main__":
    print("MCP Fixture Validation")
    print("=" * 50)
    test_cache_stampede()
    test_expired_credential()
    test_multi_action_failure()
    test_bad_deployment_unchanged()
    print("=" * 50)
    print("ALL FIXTURE TESTS PASSED ✅")
