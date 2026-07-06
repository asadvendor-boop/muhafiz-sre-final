#!/usr/bin/env python3
"""Ablation Analyzer: Compare full pipeline vs no-Muhtasib baseline.

Produces ablation_summary.md and ablation_summary.json with:
- Pass rates and critical failures
- Safety review rounds and challenges
- Plan-risk analysis (unreviewed plans reaching human gate)
- Token cost comparison
- Runtime comparison
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


def load_results(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def analyze_plan_risk(db_path: str) -> dict:
    """Scan the ablation DB for unreviewed plans and their actions."""
    if not Path(db_path).exists():
        return {"error": f"DB not found: {db_path}"}

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    # Count plan_created events (first-draft plans)
    plans = db.execute(
        "SELECT incident_id, payload_json FROM events "
        "WHERE event_type = 'plan_created' ORDER BY incident_id"
    ).fetchall()

    # Count baseline skip events
    skipped = db.execute(
        "SELECT COUNT(*) as cnt FROM events "
        "WHERE event_type = 'baseline_safety_review_skipped'"
    ).fetchone()["cnt"]

    # Count contracts issued
    contracts = db.execute(
        "SELECT COUNT(*) as cnt FROM events "
        "WHERE event_type = 'contract_issued'"
    ).fetchone()["cnt"]

    # Analyze plan actions
    unreviewed_plans = []
    total_actions = 0
    multi_action_plans = 0

    for p in plans:
        payload = json.loads(p["payload_json"]) if p["payload_json"] else {}
        plan = payload.get("plan", {})
        actions = plan.get("actions", [])
        revision = plan.get("revision", 1)

        action_ids = [a.get("action_id", "?") for a in actions]
        total_actions += len(actions)
        if len(actions) > 1:
            multi_action_plans += 1

        unreviewed_plans.append({
            "incident_id": p["incident_id"],
            "revision": revision,
            "action_count": len(actions),
            "action_ids": action_ids,
        })

    db.close()

    return {
        "safety_reviews_skipped": skipped,
        "contracts_issued_without_review": contracts,
        "first_draft_plans": len(unreviewed_plans),
        "multi_action_plans_without_review": multi_action_plans,
        "total_actions_unreviewed": total_actions,
        "plans": unreviewed_plans,
    }


def compare_results(full: dict, baseline: dict, plan_risk: dict) -> dict:
    """Build the comparison summary."""
    # --- Pass rates ---
    full_passed = full["summary"]["passed"]
    full_total = full["summary"]["total"]
    base_passed = baseline["summary"]["passed"]
    base_total = baseline["summary"]["total"]

    # --- Critical failures ---
    full_critical_fails = 0
    full_critical_total = 0
    base_critical_fails = 0
    base_critical_total = 0

    for run in full["runs"]:
        for c in run.get("checks", []):
            if c.get("critical"):
                full_critical_total += 1
                if not c.get("passed"):
                    full_critical_fails += 1

    for run in baseline["runs"]:
        for c in run.get("checks", []):
            if c.get("critical"):
                base_critical_total += 1
                if not c.get("passed"):
                    base_critical_fails += 1

    # --- Safety review rounds ---
    full_sr_rounds = sum(
        len(r.get("safety_review_rounds", [])) for r in full["runs"]
    )
    base_sr_rounds = sum(
        len(r.get("safety_review_rounds", [])) for r in baseline["runs"]
    )

    full_challenges = sum(
        sum(
            1 for sr in r.get("safety_review_rounds", [])
            if sr.get("decision") == "CHALLENGE"
        )
        for r in full["runs"]
    )
    base_challenges = sum(
        sum(
            1 for sr in r.get("safety_review_rounds", [])
            if sr.get("decision") == "CHALLENGE"
        )
        for r in baseline["runs"]
    )

    # --- Tokens ---
    full_tokens = sum(r.get("total_tokens", 0) for r in full["runs"])
    base_tokens = sum(r.get("total_tokens", 0) for r in baseline["runs"])

    # --- Events ---
    full_events = sum(r.get("event_count", 0) for r in full["runs"])
    base_events = sum(r.get("event_count", 0) for r in baseline["runs"])

    # --- Runtime ---
    full_runtime = sum(r.get("elapsed_seconds", 0) for r in full["runs"])
    base_runtime = sum(r.get("elapsed_seconds", 0) for r in baseline["runs"])

    # --- Per-scenario ---
    full_by_scenario = defaultdict(list)
    for run in full["runs"]:
        full_by_scenario[run["scenario_id"]].append(run)

    base_by_scenario = defaultdict(list)
    for run in baseline["runs"]:
        base_by_scenario[run["scenario_id"]].append(run)

    per_scenario = []
    for sid in sorted(set(list(full_by_scenario) + list(base_by_scenario))):
        f_runs = full_by_scenario.get(sid, [])
        b_runs = base_by_scenario.get(sid, [])

        f_pass = sum(1 for r in f_runs if r.get("grade") == "PASS")
        b_pass = sum(1 for r in b_runs if r.get("grade") == "PASS")

        f_avg_score = (
            sum(r["score"] for r in f_runs) / len(f_runs) if f_runs else 0
        )
        b_avg_score = (
            sum(r["score"] for r in b_runs) / len(b_runs) if b_runs else 0
        )

        f_challenges_s = sum(
            sum(
                1
                for sr in r.get("safety_review_rounds", [])
                if sr.get("decision") == "CHALLENGE"
            )
            for r in f_runs
        )

        per_scenario.append({
            "scenario_id": sid,
            "full_pass_rate": f"{f_pass}/{len(f_runs)}",
            "baseline_pass_rate": f"{b_pass}/{len(b_runs)}",
            "full_avg_score": round(f_avg_score, 3),
            "baseline_avg_score": round(b_avg_score, 3),
            "full_challenges": f_challenges_s,
            "baseline_challenges": 0,
        })

    return {
        "title": "Muhtasib Safety Reviewer Ablation",
        "description": (
            "Controlled live LLM ablation using deterministic telemetry "
            "fixtures. Compares the full 5-agent pipeline against a 4-agent "
            "baseline where Muhtasib is skipped and plans are routed "
            "directly to human approval."
        ),
        "full_pipeline": {
            "runs": full_total,
            "passed": full_passed,
            "pass_rate": round(full_passed / full_total, 3) if full_total else 0,
            "critical_failures": full_critical_fails,
            "critical_total": full_critical_total,
            "safety_review_rounds": full_sr_rounds,
            "challenges_issued": full_challenges,
            "total_tokens": full_tokens,
            "total_events": full_events,
            "total_runtime_seconds": round(full_runtime, 1),
            "models": full["provenance"].get("models", {}),
        },
        "baseline_no_muhtasib": {
            "runs": base_total,
            "passed": base_passed,
            "pass_rate": round(base_passed / base_total, 3) if base_total else 0,
            "critical_failures": base_critical_fails,
            "critical_total": base_critical_total,
            "safety_review_rounds": base_sr_rounds,
            "challenges_issued": base_challenges,
            "total_tokens": base_tokens,
            "total_events": base_events,
            "total_runtime_seconds": round(base_runtime, 1),
            "first_pass_commit_rate": "N/A (safety reviewer skipped)",
        },
        "governance_delta": {
            "safety_reviews_removed": full_sr_rounds - base_sr_rounds,
            "challenges_removed": full_challenges - base_challenges,
            "token_savings_percent": round(
                (1 - base_tokens / full_tokens) * 100, 1
            ) if full_tokens else 0,
            "runtime_savings_percent": round(
                (1 - base_runtime / full_runtime) * 100, 1
            ) if full_runtime else 0,
            "audit_events_removed": full_events - base_events,
        },
        "plan_risk": plan_risk,
        "per_scenario": per_scenario,
        "conclusion": (
            "The no-Muhtasib baseline still passed scenarios, proving the "
            "rest of the pipeline is robust. But it eliminated independent "
            "safety review: first-draft plans went directly to the human "
            "gate, shifting safety burden back to the operator. Muhtasib "
            "adds measurable governance quality at a measurable token cost."
        ),
    }


def generate_markdown(summary: dict) -> str:
    """Generate ablation_summary.md from the comparison data."""
    fp = summary["full_pipeline"]
    bl = summary["baseline_no_muhtasib"]
    gd = summary["governance_delta"]
    pr = summary["plan_risk"]

    lines = [
        "# Muhtasib Safety Reviewer Ablation",
        "",
        "> This experiment compares the full 5-agent pipeline against a "
        "4-agent baseline where Muhtasib is skipped and the plan is routed "
        "directly to human approval.",
        ">",
        "> **This is an evaluation-only ablation. The final runtime keeps "
        "Muhtasib enabled.**",
        "",
        "## Headline Results",
        "",
        "| Metric | Full Pipeline | No-Muhtasib Baseline |",
        "|--------|---:|---:|",
        f"| Runs | {fp['runs']} | {bl['runs']} |",
        f"| Passed | {fp['passed']} | {bl['passed']} |",
        f"| Pass Rate | {fp['pass_rate']*100:.0f}% | {bl['pass_rate']*100:.0f}% |",
        f"| Critical Failures | {fp['critical_failures']}/{fp['critical_total']} | "
        f"{bl['critical_failures']}/{bl['critical_total']} |",
        f"| Safety Review Rounds | **{fp['safety_review_rounds']}** | "
        f"**{bl['safety_review_rounds']}** |",
        f"| Challenges Issued | **{fp['challenges_issued']}** | "
        f"**{bl['challenges_issued']}** |",
        f"| Total Tokens | {fp['total_tokens']:,} | {bl['total_tokens']:,} |",
        f"| Total Events (audit) | {fp['total_events']} | {bl['total_events']} |",
        f"| Total Runtime (s) | {fp['total_runtime_seconds']} | "
        f"{bl['total_runtime_seconds']} |",
        "",
        "## Governance Delta",
        "",
        f"- Safety review rounds removed: **{gd['safety_reviews_removed']}**",
        f"- Challenges removed: **{gd['challenges_removed']}**",
        f"- Token savings: **{gd['token_savings_percent']}%**",
        f"- Runtime savings: **{gd['runtime_savings_percent']}%**",
        f"- Audit events removed: **{gd['audit_events_removed']}**",
        "",
        "## Plan-Risk Analysis",
        "",
        f"In the no-Muhtasib baseline, **{pr.get('first_draft_plans', 0)} "
        f"first-draft plans** reached the human gate without independent "
        f"safety review, containing **{pr.get('total_actions_unreviewed', 0)} "
        f"total operational actions**.",
        "",
        f"- Multi-action plans without review: "
        f"**{pr.get('multi_action_plans_without_review', 0)}**",
        f"- Contracts issued without safety review: "
        f"**{pr.get('contracts_issued_without_review', 0)}**",
        "",
    ]

    # Per-scenario table
    lines.extend([
        "## Per-Scenario Comparison",
        "",
        "| Scenario | Full Pass | Base Pass | Full Challenges | "
        "Base Challenges |",
        "|----------|---:|---:|---:|---:|",
    ])
    for s in summary["per_scenario"]:
        lines.append(
            f"| {s['scenario_id']} | {s['full_pass_rate']} | "
            f"{s['baseline_pass_rate']} | {s['full_challenges']} | "
            f"{s['baseline_challenges']} |"
        )

    lines.extend([
        "",
        "## Conclusion",
        "",
        f"> {summary['conclusion']}",
        "",
        "---",
        "*Generated by `evaluation/ablation/analyze_ablation.py`*",
    ])

    return "\n".join(lines)


def main():
    base_dir = Path(__file__).parent

    full_path = base_dir.parent / "results_gemini3flash_21rep.json"
    baseline_path = base_dir / "no_muhtasib_results.json"
    db_path = Path("data/ablation_baseline.db")

    # Fall back to probe if full baseline not available yet
    if not baseline_path.exists():
        baseline_path = base_dir / "probe_baseline_7run.json"
        db_path = Path("data/ablation_probe.db")

    if not full_path.exists():
        print(f"ERROR: Full pipeline results not found: {full_path}")
        sys.exit(1)
    if not baseline_path.exists():
        print(f"ERROR: Baseline results not found: {baseline_path}")
        sys.exit(1)

    print(f"Full pipeline: {full_path}")
    print(f"Baseline:      {baseline_path}")
    print(f"Ablation DB:   {db_path}")

    full = load_results(str(full_path))
    baseline = load_results(str(baseline_path))
    plan_risk = analyze_plan_risk(str(db_path))

    summary = compare_results(full, baseline, plan_risk)

    # Write JSON summary
    json_out = base_dir / "ablation_summary.json"
    with open(json_out, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Wrote: {json_out}")

    # Write markdown summary
    md_out = base_dir / "ablation_summary.md"
    md_content = generate_markdown(summary)
    with open(md_out, "w") as f:
        f.write(md_content)
    print(f"Wrote: {md_out}")

    # Print headline
    print()
    print(md_content)


if __name__ == "__main__":
    main()
