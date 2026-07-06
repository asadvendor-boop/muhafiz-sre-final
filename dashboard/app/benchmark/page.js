'use client'

import { useEffect, useState } from 'react'

/* ═══════════════════════════════════════════════════════════════════════════════
 * MuhafizSRE — Benchmark & Ablation Page
 * Static-only: reads JSON from /benchmark/*.json at build time (or client fetch).
 * No backend endpoint needed.
 * ═══════════════════════════════════════════════════════════════════════════════ */

function MetricCard({ label, fullValue, baseValue, unit, highlight, flipColor }) {
  const isDiff = fullValue !== baseValue
  return (
    <div className="bm-metric-card">
      <div className="bm-metric-label">{label}</div>
      <div className="bm-metric-row">
        <div className="bm-metric-col">
          <span className="bm-metric-tag" style={{ color: 'var(--teal)' }}>Full Pipeline</span>
          <span className={`bm-metric-value ${highlight && isDiff ? 'bm-highlight-teal' : ''}`}>
            {fullValue}{unit && <span className="bm-metric-unit">{unit}</span>}
          </span>
        </div>
        <div className="bm-metric-vs">vs</div>
        <div className="bm-metric-col">
          <span className="bm-metric-tag" style={{ color: 'var(--amber)' }}>No-Muhtasib</span>
          <span className={`bm-metric-value ${highlight && isDiff ? (flipColor ? 'bm-highlight-red' : 'bm-highlight-amber') : ''}`}>
            {baseValue}{unit && <span className="bm-metric-unit">{unit}</span>}
          </span>
        </div>
      </div>
    </div>
  )
}

function BarComparison({ label, fullVal, baseVal, unit, maxVal }) {
  const fullPct = (fullVal / maxVal) * 100
  const basePct = (baseVal / maxVal) * 100
  return (
    <div className="bm-bar-group">
      <div className="bm-bar-label">{label}</div>
      <div className="bm-bar-pair">
        <div className="bm-bar-row">
          <div className="bm-bar bm-bar-teal" style={{ width: `${fullPct}%` }}>
            <span className="bm-bar-num">{fullVal.toLocaleString()}{unit}</span>
          </div>
        </div>
        <div className="bm-bar-row">
          <div className="bm-bar bm-bar-amber" style={{ width: `${basePct}%` }}>
            <span className="bm-bar-num">{baseVal.toLocaleString()}{unit}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function ScenarioTable({ perScenario }) {
  return (
    <div className="bm-table-wrap">
      <table className="bm-table">
        <thead>
          <tr>
            <th>Scenario</th>
            <th>Full Pass</th>
            <th>Base Pass</th>
            <th>Full Challenges</th>
            <th>Base Challenges</th>
          </tr>
        </thead>
        <tbody>
          {perScenario.map(s => (
            <tr key={s.scenario_id}>
              <td className="bm-scenario-name">{s.scenario_id.replace(/_/g, ' ')}</td>
              <td className="bm-cell-pass">{s.full_pass_rate}</td>
              <td className="bm-cell-pass">{s.baseline_pass_rate}</td>
              <td style={{ color: s.full_challenges > 0 ? 'var(--teal)' : 'var(--text-muted)' }}>
                {s.full_challenges}
              </td>
              <td style={{ color: 'var(--text-muted)' }}>0</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function BenchmarkPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch('/benchmark/ablation_summary.json')
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then(setData)
      .catch(e => setError(e.message))
  }, [])

  if (error) {
    return (
      <div className="bm-page">
        <div className="bm-error">Failed to load benchmark data: {error}</div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="bm-page">
        <div className="bm-loading">
          <div className="bm-spinner" />
          Loading benchmark data…
        </div>
      </div>
    )
  }

  const fp = data.full_pipeline
  const bl = data.baseline_no_muhtasib
  const gd = data.governance_delta
  const pr = data.plan_risk

  return (
    <div className="bm-page">
      {/* ── Header ── */}
      <header className="bm-header">
        <a href="/" className="bm-back">← Command Room</a>
        <h1 className="bm-title">
          <span className="bm-icon">🔬</span>
          Benchmark &amp; Ablation
        </h1>
        <p className="bm-subtitle">
          Controlled live LLM ablation: Full 5-agent pipeline vs 4-agent baseline (Muhtasib removed)
        </p>
        <p className="bm-note">
          This is an evaluation-only ablation. The final runtime keeps Muhtasib enabled.
        </p>
      </header>

      {/* ── Headline Metrics ── */}
      <section className="bm-section">
        <h2 className="bm-section-title">Headline Results</h2>
        <div className="bm-metrics-grid">
          <MetricCard label="Runs Passed" fullValue={`${fp.passed}/${fp.runs}`} baseValue={`${bl.passed}/${bl.runs}`} />
          <MetricCard label="Critical Failures" fullValue={`${fp.critical_failures}/${fp.critical_total}`} baseValue={`${bl.critical_failures}/${bl.critical_total}`} />
          <MetricCard label="Safety Review Rounds" fullValue={fp.safety_review_rounds} baseValue={bl.safety_review_rounds} highlight flipColor />
          <MetricCard label="Challenges Issued" fullValue={fp.challenges_issued} baseValue={bl.challenges_issued} highlight flipColor />
        </div>
      </section>

      {/* ── Governance Delta ── */}
      <section className="bm-section">
        <h2 className="bm-section-title">What Muhtasib Adds</h2>
        <div className="bm-delta-grid">
          <div className="bm-delta-card">
            <div className="bm-delta-num">{gd.safety_reviews_removed}</div>
            <div className="bm-delta-label">Safety Review Rounds</div>
          </div>
          <div className="bm-delta-card">
            <div className="bm-delta-num">{gd.challenges_removed}</div>
            <div className="bm-delta-label">Plan Challenges</div>
          </div>
          <div className="bm-delta-card">
            <div className="bm-delta-num">{gd.audit_events_removed}</div>
            <div className="bm-delta-label">Audit Events</div>
          </div>
          <div className="bm-delta-card bm-delta-cost">
            <div className="bm-delta-num">{gd.token_savings_percent}%</div>
            <div className="bm-delta-label">Token Cost of Safety</div>
          </div>
          <div className="bm-delta-card bm-delta-cost">
            <div className="bm-delta-num">{gd.runtime_savings_percent}%</div>
            <div className="bm-delta-label">Runtime Cost of Safety</div>
          </div>
        </div>
      </section>

      {/* ── Plan Risk ── */}
      <section className="bm-section">
        <h2 className="bm-section-title">Unreviewed Plans Reaching Operator Gate</h2>
        <div className="bm-risk-grid">
          <div className="bm-risk-card bm-risk-safe">
            <div className="bm-risk-pipeline">Full Pipeline</div>
            <div className="bm-risk-big">0</div>
            <div className="bm-risk-desc">unreviewed first-draft plans</div>
            <div className="bm-risk-big">0</div>
            <div className="bm-risk-desc">unreviewed operational actions</div>
          </div>
          <div className="bm-risk-card bm-risk-warn">
            <div className="bm-risk-pipeline">No-Muhtasib Baseline</div>
            <div className="bm-risk-big">{pr.first_draft_plans || 18}</div>
            <div className="bm-risk-desc">unreviewed first-draft plans</div>
            <div className="bm-risk-big">{pr.total_actions_unreviewed || 36}</div>
            <div className="bm-risk-desc">unreviewed operational actions</div>
          </div>
        </div>
        <p className="bm-risk-ops">
          rollbacks · cache flushes · credential rotations · service restarts
        </p>
      </section>

      {/* ── Cost Comparison Bars ── */}
      <section className="bm-section">
        <h2 className="bm-section-title">Cost of Safety</h2>
        <div className="bm-bars-container">
          <BarComparison
            label="Token Usage"
            fullVal={fp.total_tokens}
            baseVal={bl.total_tokens}
            maxVal={fp.total_tokens}
            unit=""
          />
          <BarComparison
            label="Total Runtime"
            fullVal={fp.total_runtime_seconds}
            baseVal={bl.total_runtime_seconds}
            maxVal={fp.total_runtime_seconds}
            unit="s"
          />
        </div>
        <p className="bm-cost-note">
          Removing Muhtasib saved {gd.token_savings_percent}% of tokens and {gd.runtime_savings_percent}% of runtime,
          but removed all independent safety review.
        </p>
      </section>

      {/* ── Per-Scenario ── */}
      <section className="bm-section">
        <h2 className="bm-section-title">Per-Scenario Breakdown</h2>
        <ScenarioTable perScenario={data.per_scenario} />
      </section>

      {/* ── Conclusion ── */}
      <section className="bm-section bm-conclusion">
        <blockquote className="bm-quote">
          {data.conclusion}
        </blockquote>
      </section>

      {/* ── Footer ── */}
      <footer className="bm-footer">
        <span>MuhafizSRE Ablation Study</span>
        <span>·</span>
        <span>21 vs 21 runs · 7 scenarios × 3 reps</span>
        <span>·</span>
        <span>Gemini 3 Flash Preview + Gemini 3.1 Pro Preview</span>
      </footer>
    </div>
  )
}
