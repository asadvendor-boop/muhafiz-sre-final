"""Run targeted scenario evaluation — 4 specific scenarios × 1 rep each."""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TARGETS = [
    "bad_deployment",
    "cache_stampede",
    "expired_credential",
    "multi_action_failure",
]


async def main():
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        logger.error("Set GEMINI_API_KEY or GOOGLE_API_KEY first")
        return

    from gateway.security import Settings, ApprovalTokenManager
    from gateway.store import IncidentStore
    from evaluation.runner import run_single_scenario
    from shared.dependencies import init_dependencies
    import secrets as _s

    settings = Settings.from_env()
    store = IncidentStore()
    await store.initialize()

    # Auto-generate approval secret for evaluation if not configured
    if not settings.approval_secret or len(settings.approval_secret) < 32:
        settings.approval_secret = _s.token_hex(32)
    token_mgr = ApprovalTokenManager(settings.approval_secret)
    init_dependencies(store=store, token_manager=token_mgr, settings=settings)

    results = []
    for scenario_id in TARGETS:
        logger.info("=" * 60)
        logger.info("RUNNING: %s", scenario_id)
        logger.info("=" * 60)
        start = time.time()
        try:
            result = await run_single_scenario(
                scenario_id, store, settings, token_mgr,
            )
            elapsed = time.time() - start
            result["elapsed_seconds"] = round(elapsed, 1)
            passed = result.get("metrics", {}).get("pass", False)
            logger.info(
                "RESULT: %s — %s (%.1fs)",
                scenario_id,
                "✅ PASS" if passed else "❌ FAIL",
                elapsed,
            )
        except Exception as e:
            elapsed = time.time() - start
            result = {
                "scenario_id": scenario_id,
                "error": str(e),
                "elapsed_seconds": round(elapsed, 1),
            }
            logger.error("CRASH: %s — %s (%.1fs)", scenario_id, e, elapsed)
        results.append(result)

    # Write results
    output = {
        "run_type": "targeted_4_scenario",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.get("metrics", {}).get("pass")),
            "failed": sum(1 for r in results if not r.get("metrics", {}).get("pass") and "error" not in r),
            "crashed": sum(1 for r in results if "error" in r),
        },
    }
    outpath = "evaluation/targeted_results.json"
    with open(outpath, "w") as f:
        json.dump(output, f, indent=2, default=str)
    logger.info("Results written to %s", outpath)

    # Print summary table
    print("\n" + "=" * 60)
    print("TARGETED EVALUATION SUMMARY")
    print("=" * 60)
    for r in results:
        sid = r.get("scenario_id", r.get("metrics", {}).get("scenario_id", "?"))
        if "error" in r:
            status = f"💥 CRASH: {r['error'][:60]}"
        elif r.get("metrics", {}).get("pass"):
            status = "✅ PASS"
        else:
            status = "❌ FAIL"
            # Show which checks failed
            metrics = r.get("metrics", {})
            failures = [k for k, v in metrics.items() if k.startswith("check_") and not v]
            if failures:
                status += f" ({', '.join(failures)})"
        print(f"  {sid:25s} {status}")
    print(f"\nPassed: {output['summary']['passed']}/{output['summary']['total']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
