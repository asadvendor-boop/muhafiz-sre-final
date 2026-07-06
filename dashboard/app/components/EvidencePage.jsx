"use client";

import AuditTab from "./AuditTab";

export default function EvidencePage({ evalData, events, incidentId, onNavigate }) {
  /* ── Proof data derivation ── */
  const proofUnavailable = !evalData || evalData === "unavailable";
  const evalOk = evalData && evalData !== "unavailable";

  const passed = evalOk ? evalData.workflows_passed : null;
  const total = evalOk ? evalData.total_workflows : null;
  const sealVerified = evalOk && typeof passed === 'number' && typeof total === 'number' && total > 0 && passed === total;

  const bigText = evalOk ? `${passed} / ${total}` : "— / —";
  const sealLabel = sealVerified ? "PASS" : "PENDING";
  const sealStatus = sealVerified ? "✓ VERIFIED" : "◌ PENDING";

  const unauthorized = evalOk ? (evalData.unauthorized_executions ?? "—") : "—";
  const chains = evalOk ? (evalData.valid_audit_chains ?? "—") : "—";
  const retryRecovery = evalOk && evalData.retry_recovery
    ? `${evalData.retry_recovery.attempted}/${evalData.retry_recovery.recovered}`
    : "—";

  const CHAIN_NODES = ["Genesis", "Contract", "Decision", "Receipt", "Seal"];

  return (
    <div className="evidence-page">
      {/* ── Page Hero ── */}
      <div className="page-hero">
        <span className="page-hero__icon">📊</span>
        <h2 className="page-hero__title">Proof Ledger</h2>
        <p className="page-hero__subtitle">
          Every agent decision is sealed into a tamper-evident chain.
        </p>
      </div>

      {/* ── Benchmark Seal — THE FOCAL OBJECT ── */}
      <div className={`evidence-seal${sealVerified ? " evidence-seal--verified" : ""}`}>
        <div className="evidence-seal__badge">BENCHMARK SEAL</div>
        <div className="evidence-seal__result">
          <span className="evidence-seal__big">{bigText}</span>
          <span className={`evidence-seal__label${sealVerified ? " evidence-seal__label--pass" : ""}`}>
            {sealLabel}
          </span>
        </div>
        <div className="evidence-seal__stats">
          <span>{chains !== "—" ? `${chains} hash chains intact` : "hash chains pending"}</span>
          <span className="evidence-seal__stats-sep">·</span>
          <span>{unauthorized} unauthorized</span>
        </div>
        <div className={`evidence-seal__status${sealVerified ? " evidence-seal__status--verified" : ""}`}>
          {sealStatus}
        </div>
      </div>

      {/* ── Supporting Proof Chips ── */}
      <div className="evidence-chips">
        <span className="evidence-chip">503 → 200 Recovery</span>
        <span className="evidence-chip">{unauthorized} Unauthorized</span>
        <span className="evidence-chip">403 / 409 Blocked</span>
        <span className="evidence-chip">{chains} Valid Chains</span>
        <span className="evidence-chip">{retryRecovery} Retry Recovery</span>
      </div>

      {/* ── Conceptual Hash-Chain Visualization ── */}
      <div className="evidence-chain">
        {CHAIN_NODES.map((node, i) => (
          <span key={node} className="evidence-chain__segment">
            {i > 0 && <span className="evidence-chain__link">→</span>}
            <span className={`evidence-chain__node${node === "Seal" ? " evidence-chain__node--sealed" : ""}`}>
              {node === "Seal" ? "Seal ✓" : node}
            </span>
          </span>
        ))}
      </div>

      {/* ── Provenance (subtle, under chain) ── */}
      <div className="evidence-page__provenance">
        from 21-run evaluation benchmark
      </div>

      {/* ── Audit Chain or No-Incident CTA ── */}
      {incidentId ? (
        <>
          <h3 className="evidence-page__section-heading">
            Audit Chain — {incidentId}
          </h3>
          <AuditTab events={events} incidentId={incidentId} />
        </>
      ) : (
        <div className="evidence-page__no-incident">
          <p>Benchmark proof is already sealed.</p>
          <p>Launch a guided incident to inspect its individual audit chain.</p>
          <div className="evidence-page__actions">
            <button
              className="evidence-page__cta evidence-page__cta--primary"
              onClick={() => onNavigate("launch")}
            >
              🚀 Launch Guided Incident
            </button>
            <button
              className="evidence-page__cta evidence-page__cta--secondary"
              onClick={() => onNavigate("incidents")}
            >
              📋 Open Docket
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
