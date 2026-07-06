"""evaluation/report.py – Evaluation Report Generator for MuhafizSRE"""
import json
from datetime import datetime, timezone
from pathlib import Path


def generate_report(
    results_path: str = "evaluation/results.json",
    output_path: str = "evaluation/REPORT.md",
) -> str:
    """Generate a markdown evaluation report from results.json.

    Reads the evaluation results file produced by the runner, and
    generates a comprehensive markdown report with summary table,
    per-scenario details, provenance, and overall pass rate.

    Args:
        results_path: Path to the JSON results file from the evaluation
            runner.  Defaults to ``evaluation/results.json``.
        output_path: Path where the markdown report will be written.
            Defaults to ``evaluation/REPORT.md``.

    Returns:
        The generated markdown report as a string.  Also writes it to
        ``output_path``.
    """
    results_file = Path(results_path)
    if not results_file.exists():
        raise FileNotFoundError(f"Results file not found: {results_path}")

    data = json.loads(results_file.read_text())

    lines: list[str] = []
    lines.append("# MuhafizSRE Evaluation Report")
    lines.append("")

    # ── Provenance ──────────────────────────────────────────────────────────
    provenance = data.get("provenance", {})
    if provenance:
        lines.append("## Provenance")
        lines.append("")
        models = provenance.get("models", {})
        if isinstance(models, dict):
            lines.append(
                f"- **Models:** Speed=`{models.get('speed', 'N/A')}`, "
                f"Analytical=`{models.get('analytical', 'N/A')}`, "
                f"Safety=`{models.get('safety', 'N/A')}`"
            )
        else:
            lines.append(f"- **Model:** {provenance.get('model', 'N/A')}")
        lines.append(f"- **Git SHA:** `{provenance.get('git_sha', 'N/A')}`")
        lines.append(f"- **Timestamp:** {provenance.get('timestamp', 'N/A')}")
        lines.append(f"- **Repetitions:** {provenance.get('repetitions', 'N/A')}")
        lines.append(
            f"- **Total Scenarios:** {provenance.get('total_scenarios', 'N/A')}"
        )
        lines.append(
            f"- **Total Runs:** {provenance.get('total_runs', 'N/A')}"
        )
        lines.append("")

    # ── Summary ─────────────────────────────────────────────────────────────
    summary = data.get("summary", {})
    lines.append("## Summary")
    lines.append("")
    pass_rate = summary.get("pass_rate", 0)
    lines.append(f"- **Pass Rate:** {pass_rate:.0%}")
    lines.append(f"- **Passed:** {summary.get('passed', 0)}/{summary.get('total', 0)}")
    lines.append(f"- **Failed:** {summary.get('failed', 0)}/{summary.get('total', 0)}")
    lines.append("")

    # Overall grade
    if pass_rate >= 1.0:
        lines.append("> ✅ **ALL SCENARIOS PASSED**")
    elif pass_rate >= 0.7:
        lines.append(f"> ⚠️ **PARTIAL PASS** — {pass_rate:.0%} pass rate")
    else:
        lines.append(f"> ❌ **FAILING** — {pass_rate:.0%} pass rate")
    lines.append("")

    # ── Scenario Results Table ──────────────────────────────────────────────
    runs = data.get("runs", data.get("scenarios", []))

    lines.append("## Scenario Results")
    lines.append("")
    lines.append("| # | Scenario | Repetition | Grade | Score | Events | Time | Key Metrics |")
    lines.append("|---|----------|------------|-------|-------|--------|------|-------------|")

    for idx, run in enumerate(runs, 1):
        grade = run.get("grade", "N/A")
        grade_emoji = "✅" if grade == "PASS" else "❌"
        scenario_id = run.get("scenario_id", "unknown")
        repetition = run.get("repetition", "-")
        score = run.get("score", 0)
        event_count = run.get("event_count", 0)
        elapsed = run.get("elapsed_seconds", 0)

        # Summarize key metrics from checks
        checks = run.get("checks", [])
        failed_checks = [c["name"] for c in checks if not c.get("passed")]
        key_metrics = ", ".join(failed_checks[:3]) if failed_checks else "all passed"

        lines.append(
            f"| {idx} | {scenario_id} | {repetition} | "
            f"{grade_emoji} {grade} | {score:.0%} | "
            f"{event_count} | {elapsed:.1f}s | {key_metrics} |"
        )

    lines.append("")

    # ── Per-Scenario Details ────────────────────────────────────────────────
    lines.append("## Per-Scenario Details")
    lines.append("")

    for run in runs:
        scenario_id = run.get("scenario_id", "unknown")
        grade = run.get("grade", "N/A")
        repetition = run.get("repetition", "-")
        grade_emoji = "✅" if grade == "PASS" else "❌"
 
        lines.append(f"### {scenario_id} (repetition={repetition}) — {grade_emoji} {grade}")
        lines.append("")
        lines.append(f"- **Incident:** `{run.get('incident_id', 'N/A')}`")
        lines.append(f"- **Score:** {run.get('score', 0):.0%}")
        lines.append(f"- **Events:** {run.get('event_count', 0)}")
        lines.append(f"- **Time:** {run.get('elapsed_seconds', 0):.1f}s")
        run_models = run.get("models", {})
        if isinstance(run_models, dict) and run_models:
            lines.append(
                f"- **Models:** Speed=`{run_models.get('speed', 'N/A')}`, "
                f"Analytical=`{run_models.get('analytical', 'N/A')}`, "
                f"Safety=`{run_models.get('safety', 'N/A')}`"
            )
        elif run.get("model"):
            lines.append(f"- **Model:** {run['model']}")
        lines.append("")

        checks = run.get("checks", [])
        if checks:
            lines.append("| Check | Expected | Actual | Result |")
            lines.append("|-------|----------|--------|--------|")
            for check in checks:
                icon = "✅" if check["passed"] else "❌"
                critical = " ⚠️" if check.get("critical") else ""
                lines.append(
                    f"| {check['name']}{critical} | "
                    f"`{check['expected']}` | "
                    f"`{check['actual']}` | {icon} |"
                )
            lines.append("")

    # ── Footer ──────────────────────────────────────────────────────────────
    lines.append("---")
    lines.append(
        f"*Report generated at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}*"
    )
    lines.append("")

    report = "\n".join(lines)

    # Write to file
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(report)

    return report


if __name__ == "__main__":
    report = generate_report()
    print(report)
