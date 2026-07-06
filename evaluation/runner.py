"""evaluation/runner.py – Evaluation Runner for MuhafizSRE"""
import argparse
import asyncio
import json
import logging
import os

# ═══════════════════════════════════════════════════════════════════════════════
# BENCHMARK PROTECTION — force deterministic environment
# ═══════════════════════════════════════════════════════════════════════════════
# These overrides MUST be set before any gateway/shared imports that might
# read environment variables.  This guarantees that the 21-run benchmark
# never performs real HTTP mutations or mixes sandbox results into the
# evaluation dataset, regardless of what the user's .env or compose file
# contains.
os.environ["MUHAFIZ_EXECUTION_MODE"] = "simulated"
os.environ["MUHAFIZ_TELEMETRY_MODE"] = "fixture"
# Remove any stale VICTIM_SERVICE_URL to prevent accidental real health checks
os.environ.pop("VICTIM_SERVICE_URL", None)
# ═══════════════════════════════════════════════════════════════════════════════
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from gateway.models import IncidentStatus
from gateway.security import Settings, ApprovalTokenManager
from gateway.store import IncidentStore
from evaluation.scenarios import SCENARIOS, get_scenario
from evaluation.metrics import evaluate_scenario
from shared.chain_verifier import verify_chain

logger = logging.getLogger(__name__)


async def _approve_via_production_path(
    incident_id: str,
    run_id: str,
    contract: dict,
    store: IncidentStore,
    token_manager: ApprovalTokenManager,
) -> bool:
    """Exercise the same atomic approval path used by the production API.

    1. Reconstructs the HMAC token from contract claims.
    2. Runs validate_decision_request() — the full 10-check validation
       (signature, expiry, digest, incident/contract/revision/plan_hash/
       nonce match, contract status, incident status).
    3. Calls claim_approval() — the atomic single-winner transaction.

    Returns True if approval was successfully claimed.
    """
    from gateway.security import validate_decision_request

    claims_json = contract.get("claims_json", "")
    if not claims_json:
        logger.error("Contract %s has no claims_json", contract["contract_id"])
        return False

    claims = json.loads(claims_json)

    # Reconstruct the HMAC token — same path the dashboard UI uses
    token = token_manager.reconstruct_token(claims)

    # Run the full production validation (10 checks)
    incident = await store.get_incident(incident_id)
    if not incident:
        logger.error("Incident %s not found for approval", incident_id)
        return False

    valid, error_msg = validate_decision_request(
        token_manager=token_manager,
        token=token,
        claims=claims,
        contract=contract,
        incident=incident,
    )
    if not valid:
        logger.error(
            "Evaluation approval validation failed for %s: %s",
            incident_id, error_msg,
        )
        return False

    # Atomic single-winner claim — only one concurrent caller succeeds
    decision_payload = {
        "action": "APPROVE",
        "contract_id": contract["contract_id"],
        "revision": contract["revision"],
        "operator": "eval-runner",
        "feedback": "Auto-approved by evaluation harness",
    }

    claimed, reason, _event = await store.claim_approval(
        incident_id=incident_id,
        contract_id=contract["contract_id"],
        revision=contract["revision"],
        approved_by="eval-runner",
        run_id=run_id,
        decision_payload=decision_payload,
    )

    if not claimed:
        logger.warning(
            "claim_approval failed for %s: %s", incident_id, reason,
        )
    return claimed


async def run_single_scenario(
    scenario_id: str,
    store: IncidentStore,
    settings: Settings,
    token_manager: ApprovalTokenManager,
) -> dict:
    """Run a single evaluation scenario end-to-end.

    Creates an incident from the scenario alert, drives it through the
    Phase 1 (triage → investigation → planning) pipeline, applies the
    scenario's human policy using the production authorization flow
    (HMAC token + claim_approval), optionally runs Phase 2 execution,
    and then evaluates the outcome against the scenario expectations.

    Args:
        scenario_id: Identifier of the scenario to run.
        store: The incident persistence store.
        settings: Application settings (secrets, TTLs, etc.).
        token_manager: HMAC token manager for approval validation.

    Returns:
        A dict with evaluation metrics, timing, and incident metadata.
        Contains an ``"error"`` key if the scenario is not found.
    """
    scenario = get_scenario(scenario_id)
    if scenario is None:
        return {"error": f"Scenario {scenario_id} not found"}

    logger.info("Starting scenario: %s", scenario_id)
    start_time = time.time()

    # Import gateway pipeline functions (deferred to avoid circular imports)
    from gateway.app import _run_phase1_pipeline

    incident_id = f"EVAL-{uuid.uuid4().hex[:8].upper()}"

    # Create incident
    incident = await store.create_incident(
        incident_id=incident_id,
        alert=scenario.alert,
        scenario_id=scenario_id,
    )

    # Claim pipeline run
    run = await store.claim_pipeline_run(
        incident_id=incident_id,
        phase="phase1",
        revision=1,
        start_stage="triage",
        input_data={"alert": scenario.alert.model_dump(mode="json")},
    )
    run_id = run["run_id"]

    # Set scenario context for MCP telemetry server (Fix #6)
    os.environ["MUHAFIZ_SCENARIO_ID"] = scenario_id

    # Run Phase 1 pipeline
    await _run_phase1_pipeline(incident_id, run_id, scenario.alert)

    # If human policy is set and status is AWAITING_APPROVAL, apply policy
    incident = await store.get_incident(incident_id)
    if (
        scenario.human_policy
        and incident
        and incident.get("status") == IncidentStatus.AWAITING_APPROVAL.value
    ):
        contract = await store.get_active_contract(incident_id)
        if contract:
            if scenario.human_policy.value == "AUTO_APPROVE":
                # Use the production authorization flow:
                # HMAC token generation + atomic claim_approval()
                claimed = await _approve_via_production_path(
                    incident_id=incident_id,
                    run_id=run_id,
                    contract=contract,
                    store=store,
                    token_manager=token_manager,
                )
                if claimed:
                    # Run Phase 2 execution (same as production background task)
                    from gateway.app import _run_phase2_execution
                    await _run_phase2_execution(incident_id, run_id, contract)

            elif scenario.human_policy.value == "AUTO_REJECT":
                # Rejection: transition contract and incident state
                await store.transition_contract(
                    incident_id=incident_id,
                    revision=contract["revision"],
                    from_status="ISSUED",
                    to_status="REJECTED",
                )
                await store.transition_incident(
                    incident_id=incident_id,
                    from_status="AWAITING_APPROVAL",
                    to_status="REJECTED",
                )

    # Allow brief settling time for any background completion
    await asyncio.sleep(0.5)

    # Collect final state
    final_incident = await store.get_incident(incident_id)
    events = await store.get_events(incident_id)
    contract = await store.get_latest_contract(incident_id)

    elapsed = time.time() - start_time

    # Chain-replay audit verification
    chain_result = await verify_chain(store, incident_id)

    # Evaluate against scenario expectations
    metrics = evaluate_scenario(
        scenario, final_incident or {}, events, contract,
        chain_valid=chain_result.valid,
    )
    metrics["elapsed_seconds"] = elapsed
    metrics["incident_id"] = incident_id
    metrics["event_count"] = len(events)
    metrics["chain_event_count"] = chain_result.event_count
    metrics["chain_valid"] = chain_result.valid

    # ── Extract token usage from durable telemetry events ──────────
    usage_events = [
        e for e in events
        if e.get("event_type") == "agent_usage_telemetry"
    ]
    usage_by_agent: dict[str, dict] = {}
    run_total_tokens = 0
    for ue in usage_events:
        try:
            payload = json.loads(ue.get("payload_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        agent = payload.get("agent", "unknown")
        if agent not in usage_by_agent:
            usage_by_agent[agent] = {
                "model": payload.get("model", ""),
                "thinking_levels": set(),
                "invocations": 0,
                "prompt_tokens": 0,
                "candidates_tokens": 0,
                "thoughts_tokens": 0,
                "total_tokens": 0,
            }
        entry = usage_by_agent[agent]
        entry["invocations"] += 1
        tl = payload.get("thinking_level", "")
        if tl:
            entry["thinking_levels"].add(tl)
        entry["prompt_tokens"] += payload.get("prompt_tokens", 0)
        entry["candidates_tokens"] += payload.get("candidates_tokens", 0)
        entry["thoughts_tokens"] += payload.get("thoughts_tokens", 0)
        entry["total_tokens"] += payload.get("total_tokens", 0)
        run_total_tokens += payload.get("total_tokens", 0)

    # Convert thinking_levels sets to sorted lists for JSON serialization
    for entry in usage_by_agent.values():
        entry["thinking_levels"] = sorted(entry["thinking_levels"])

    metrics["usage_by_agent"] = usage_by_agent
    metrics["total_tokens"] = run_total_tokens

    logger.info(
        "Scenario %s: %s (%.1fs, %d events)",
        scenario_id,
        metrics["grade"],
        elapsed,
        len(events),
    )

    return metrics


async def run_all_scenarios(
    store: IncidentStore,
    settings: Settings,
    token_manager: ApprovalTokenManager,
) -> dict:
    """Run all evaluation scenarios and produce a summary report.

    Iterates through every registered scenario sequentially, collects
    individual results, and returns an aggregate summary with pass/fail
    counts and overall pass rate.

    Args:
        store: The incident persistence store.
        settings: Application settings (secrets, TTLs, etc.).
        token_manager: HMAC token manager for approval validation.

    Returns:
        A dict with ``"summary"`` (counts + pass_rate) and ``"scenarios"``
        (list of per-scenario metric dicts).
    """
    results: list[dict] = []
    for scenario in SCENARIOS:
        result = await run_single_scenario(
            scenario.id, store, settings, token_manager,
        )
        results.append(result)

    passed = sum(1 for r in results if r.get("grade") == "PASS")
    total = len(results)

    return {
        "summary": {
            "passed": passed,
            "failed": total - passed,
            "total": total,
            "pass_rate": passed / total if total > 0 else 0.0,
        },
        "scenarios": results,
    }


def _get_git_sha() -> str:
    """Return the current short git SHA, or 'unknown' on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the evaluation runner."""
    parser = argparse.ArgumentParser(
        description="MuhafizSRE Evaluation Runner — "
        "runs 7 scenarios × N repetitions.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["mock", "live"],
        default="live",
        help=(
            "mock: no API key, calls real tool functions with scripted args. "
            "live: full LLM evaluation (default: live)"
        ),
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="Number of repetitions per scenario (default: 3)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="evaluation/results.json",
        help="Output path for results JSON (default: evaluation/results.json)",
    )
    parser.add_argument(
        "--scenarios",
        type=str,
        nargs="+",
        default=None,
        help=(
            "Run only these scenario IDs (space-separated). "
            "Default: all 7 scenarios."
        ),
    )
    return parser.parse_args()


async def _run_mock_mode(args: argparse.Namespace) -> None:
    """Mock mode: no API key, calls real tool functions with scripted args.

    Validates orchestration, state machine, event chain, and contract logic
    without spending any LLM tokens.
    """
    import sys
    from unittest.mock import patch
    from evaluation.mock_executor import create_mock_agent_runner

    # Mock mode doesn't need a real API key
    os.environ.setdefault("GEMINI_API_KEY", "mock-not-needed")
    os.environ.setdefault("MUHAFIZ_DEFAULT_MODEL", "gemini-2.0-flash")

    settings = Settings.from_env()
    store = IncidentStore()
    await store.initialize()

    import secrets as _s
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        settings.approval_secret = _s.token_hex(32)
    token_manager = ApprovalTokenManager(settings.approval_secret)
    from shared.dependencies import init_dependencies
    init_dependencies(store=store, token_manager=token_manager, settings=settings)

    git_sha = _get_git_sha()
    timestamp = datetime.now(timezone.utc).isoformat()
    repeats = args.repeats

    all_runs: list[dict] = []
    scenarios_to_run = SCENARIOS
    if args.scenarios:
        scenario_ids = set(args.scenarios)
        scenarios_to_run = [s for s in SCENARIOS if s.id in scenario_ids]
        missing = scenario_ids - {s.id for s in scenarios_to_run}
        if missing:
            logger.error("Unknown scenarios: %s", missing)
            sys.exit(1)

    total_runs = 0
    total_passed = 0

    for rep in range(1, repeats + 1):
        logger.info("═══ MOCK Repetition %d/%d ═══", rep, repeats)

        for idx, scenario in enumerate(scenarios_to_run, 1):
            total_runs += 1
            scenario_id = scenario.id
            logger.info(
                "[MOCK] Running scenario %s (rep=%d) [%d/%d]",
                scenario_id, rep,
                (rep - 1) * len(scenarios_to_run) + idx,
                len(scenarios_to_run) * repeats,
            )

            # Create a fresh mock runner per scenario
            mock_runner = create_mock_agent_runner(scenario_id)

            # Patch _run_single_agent to use the mock executor
            with patch(
                "gateway.app._run_single_agent",
                side_effect=mock_runner.run_agent,
            ):
                result = await run_single_scenario(
                    scenario_id, store, settings, token_manager,
                )

            result["repetition"] = rep
            result["mode"] = "mock"
            result["mock_call_counts"] = mock_runner.call_counts
            result["git_sha"] = git_sha
            result["timestamp"] = timestamp
            all_runs.append(result)

            grade = result.get("grade", "FAIL")
            # Extract terminal state from evaluator checks
            status = "?"
            for chk in result.get("checks", []):
                if chk.get("name") == "terminal_state":
                    status = chk.get("actual", "?")
                    break
            result["terminal_status"] = status

            if grade == "PASS":
                total_passed += 1
                logger.info(
                    "  ✅ %s → %s (PASS)", scenario_id, status,
                )
            else:
                failed_checks = [
                    chk["name"] for chk in result.get("checks", [])
                    if not chk.get("passed")
                ]
                logger.error(
                    "  ❌ %s → %s (FAIL: %s)",
                    scenario_id, status, failed_checks,
                )

    # Build output
    output = {
        "provenance": {
            "mode": "mock",
            "git_sha": git_sha,
            "timestamp": timestamp,
            "repetitions": repeats,
            "total_scenarios": len(scenarios_to_run),
            "total_runs": total_runs,
        },
        "summary": {
            "passed": total_passed,
            "failed": total_runs - total_passed,
            "total": total_runs,
            "pass_rate": total_passed / total_runs if total_runs > 0 else 0.0,
        },
        "runs": all_runs,
    }

    results_path = Path(args.output)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(output, indent=2, default=str))

    print()
    print("=" * 60)
    print(f"MOCK PIPELINE: {total_passed}/{total_runs} passed")
    print("=" * 60)
    for r in all_runs:
        icon = "✅" if r.get("grade") == "PASS" else "❌"
        status = r.get("terminal_status", "?")
        score = r.get("score", 0)
        events = r.get("event_count", 0)
        print(f"  {icon} {r['scenario_id']:25s} → {status:20s} ({score:.0%}, {events} events)")
    print("=" * 60)
    print(f"Results → {results_path}")

    if total_passed < total_runs:
        sys.exit(1)


async def main() -> None:
    """CLI entry point: run 7 scenarios × N repetitions evaluation runs.

    Supports two modes:
      --mode mock: No API key, calls real tool functions with scripted args.
      --mode live: Full LLM evaluation with Gemini.
    """
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.mode == "mock":
        await _run_mock_mode(args)
        return

    # ── Live mode (requires API key) ──────────────────────────────────────
    import sys
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        logger.error(
            "Set GEMINI_API_KEY or GOOGLE_API_KEY before running evaluation."
        )
        sys.exit(1)

    settings = Settings.from_env()
    store = IncidentStore()
    await store.initialize()

    # Auto-generate approval secret for evaluation if not configured
    import secrets as _s
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        settings.approval_secret = _s.token_hex(32)
    token_manager = ApprovalTokenManager(settings.approval_secret)
    from shared.dependencies import init_dependencies
    init_dependencies(store=store, token_manager=token_manager, settings=settings)

    # Repetitions measure variance across identical model configuration
    repeats = args.repeats
    models = {
        "speed": settings.speed_model,
        "analytical": settings.analytical_model,
        "safety": settings.safety_model,
        "default_fallback": settings.default_model,
    }
    git_sha = _get_git_sha()
    timestamp = datetime.now(timezone.utc).isoformat()

    all_runs: list[dict] = []
    total_passed = 0
    total_runs = 0

    scenarios_to_run = SCENARIOS
    if args.scenarios:
        scenario_ids = set(args.scenarios)
        scenarios_to_run = [s for s in SCENARIOS if s.id in scenario_ids]
        missing = scenario_ids - {s.id for s in scenarios_to_run}
        if missing:
            logger.error("Unknown scenarios: %s", missing)
            sys.exit(1)

    for rep in range(1, repeats + 1):
        logger.info("═══ Repetition %d/%d ═══", rep, repeats)

        for scenario in scenarios_to_run:
            total_runs += 1
            logger.info(
                "Running scenario %s (rep=%d) [%d/%d]",
                scenario.id, rep, total_runs,
                len(scenarios_to_run) * repeats,
            )
            result = await run_single_scenario(
                scenario.id, store, settings, token_manager,
            )
            result["repetition"] = rep
            result["models"] = models
            result["git_sha"] = git_sha
            result["timestamp"] = timestamp
            all_runs.append(result)
            if result.get("grade") == "PASS":
                total_passed += 1

    # ── Aggregate safety review retry metrics across ALL rounds ─────────
    total_sr_rounds = 0
    first_pass_commits = 0
    retry_recoveries = 0
    retry_attempts = 0
    for run in all_runs:
        for sr in run.get("safety_review_rounds", []):
            total_sr_rounds += 1
            if sr.get("first_pass_commit", True):
                first_pass_commits += 1
            if sr.get("retry_used", False):
                retry_attempts += 1
                # Retry recovery = retry_used AND decision is not ESCALATE
                if sr.get("decision", "") != "ESCALATE":
                    retry_recoveries += 1

    first_pass_rate = first_pass_commits / total_sr_rounds if total_sr_rounds > 0 else 1.0
    retry_recovery_rate = retry_recoveries / retry_attempts if retry_attempts > 0 else 0.0
    workflow_pass_rate = total_passed / total_runs if total_runs > 0 else 0.0

    # ── Aggregate token usage across ALL runs ───────────────────────
    agg_usage: dict[str, dict] = {}
    grand_total_tokens = 0
    for run in all_runs:
        for agent, usage in run.get("usage_by_agent", {}).items():
            if agent not in agg_usage:
                agg_usage[agent] = {
                    "model": usage.get("model", ""),
                    "thinking_levels": set(),
                    "invocations": 0,
                    "prompt_tokens": 0,
                    "candidates_tokens": 0,
                    "thoughts_tokens": 0,
                    "total_tokens": 0,
                }
            entry = agg_usage[agent]
            entry["invocations"] += usage.get("invocations", 0)
            # Merge thinking levels from the per-run list
            for tl in usage.get("thinking_levels", []):
                entry["thinking_levels"].add(tl)
            entry["prompt_tokens"] += usage.get("prompt_tokens", 0)
            entry["candidates_tokens"] += usage.get("candidates_tokens", 0)
            entry["thoughts_tokens"] += usage.get("thoughts_tokens", 0)
            entry["total_tokens"] += usage.get("total_tokens", 0)
        grand_total_tokens += run.get("total_tokens", 0)

    # Convert thinking_levels sets to sorted lists for JSON serialization
    for entry in agg_usage.values():
        entry["thinking_levels"] = sorted(entry["thinking_levels"])

    # Build output
    output = {
        "provenance": {
            "mode": "live",
            "models": models,
            "git_sha": git_sha,
            "timestamp": timestamp,
            "repetitions": repeats,
            "total_scenarios": len(SCENARIOS),
            "total_runs": total_runs,
        },
        "summary": {
            "passed": total_passed,
            "failed": total_runs - total_passed,
            "total": total_runs,
            "pass_rate": workflow_pass_rate,
        },
        "safety_review_metrics": {
            "total_safety_review_rounds": total_sr_rounds,
            "first_pass_commits": first_pass_commits,
            "first_pass_commit_rate": first_pass_rate,
            "retry_attempts": retry_attempts,
            "retry_recoveries": retry_recoveries,
            "retry_recovery_rate": retry_recovery_rate,
            "workflow_pass_rate": workflow_pass_rate,
        },
        "token_usage": {
            "usage_by_agent": agg_usage,
            "grand_total_tokens": grand_total_tokens,
        },
        "runs": all_runs,
    }

    results_path = Path(args.output)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(output, indent=2, default=str))
    logger.info(
        "Evaluation complete: %d/%d passed (%.0f%%). Results → %s",
        total_passed,
        total_runs,
        (total_passed / total_runs * 100) if total_runs > 0 else 0,
        results_path,
    )
    logger.info(
        "Safety review metrics: first_pass_commit_rate=%.1f%% (%d/%d rounds), "
        "retry_recovery_rate=%.1f%% (%d/%d retries), "
        "workflow_pass_rate=%.1f%% (%d/%d runs)",
        first_pass_rate * 100, first_pass_commits, total_sr_rounds,
        retry_recovery_rate * 100, retry_recoveries, retry_attempts,
        workflow_pass_rate * 100, total_passed, total_runs,
    )
    logger.info(
        "Token usage: grand_total=%d tokens across %d runs",
        grand_total_tokens, total_runs,
    )
    for agent, usage in sorted(agg_usage.items()):
        logger.info(
            "  %s: %d invocations, %d total tokens "
            "(prompt=%d, candidates=%d, thoughts=%d)",
            agent, usage["invocations"], usage["total_tokens"],
            usage["prompt_tokens"], usage["candidates_tokens"],
            usage["thoughts_tokens"],
        )


if __name__ == "__main__":
    asyncio.run(main())
