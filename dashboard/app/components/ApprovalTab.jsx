"use client";
/* ═══════════════════════════════════════════════════════════════════════════════
 * ApprovalTab — Operator authorization gate with exact-plan verification
 * ═══════════════════════════════════════════════════════════════════════════════
 * Council Recommendation: derived from structured events (not room messages)
 *   via deriveCouncilRecommendation(events, contract.revision).
 * Confirmation modal: focus-trapped, Escape-closable, focus-restoring.
 * Expired contract: "REVALIDATION REQUIRED", Approve disabled.
 * For terminal statuses: delegates to ResolutionRecord.
 * ═══════════════════════════════════════════════════════════════════════════════ */

import { useState, useEffect, useRef, useCallback } from "react";
import { AGENT_PERSONAS, TERMINAL_STATUSES } from "../personas";
import {
  deriveCouncilRecommendation,
  eventPayload,
} from "../lib/eventSelectors";
import ResolutionRecord from "./ResolutionRecord";

const API_BASE = "/api";

async function apiFetch(path, opts = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...opts.headers },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json();
}

function truncHash(hash) {
  if (!hash) return "--------";
  return hash.substring(0, 8);
}

function formatTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function relativeTime(iso) {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return hrs < 24 ? `${hrs}h ago` : `${Math.floor(hrs / 24)}d ago`;
}

/* ── Confirmation Modal with Focus Trap ──────────────────────────────────── */
function ConfirmationModal({
  contractId,
  revision,
  planHash,
  actionCount,
  riskLevel,
  onConfirm,
  onCancel,
}) {
  const modalRef = useRef(null);
  const previousFocusRef = useRef(null);

  useEffect(() => {
    previousFocusRef.current = document.activeElement;
    if (modalRef.current) {
      const firstFocusable = modalRef.current.querySelector(
        'button, [tabindex]:not([tabindex="-1"])'
      );
      if (firstFocusable) firstFocusable.focus();
    }
    return () => {
      if (previousFocusRef.current) previousFocusRef.current.focus();
    };
  }, []);

  useEffect(() => {
    function handleKeyDown(e) {
      if (e.key === "Escape") {
        onCancel();
        return;
      }
      // Focus trap
      if (e.key === "Tab" && modalRef.current) {
        const focusable = modalRef.current.querySelectorAll(
          'button, [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onCancel]);

  return (
    <div
      className="approval-modal-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
      role="dialog"
      aria-modal="true"
      aria-label="Confirm approval"
    >
      <div className="approval-modal" ref={modalRef}>
        <div className="approval-modal__header">
          <span className="approval-modal__icon">🔐</span>
          <span>Confirm Exact Plan Approval</span>
        </div>
        <div className="approval-modal__body">
          <p className="approval-modal__warning">
            You are authorizing execution of the following contract.
            This action cannot be undone.
          </p>
          <div className="approval-modal__details">
            <div className="approval-modal__detail">
              <span className="approval-modal__detail-label">Contract</span>
              <span className="approval-modal__detail-value mono">
                {contractId}
              </span>
            </div>
            <div className="approval-modal__detail">
              <span className="approval-modal__detail-label">Revision</span>
              <span className="approval-modal__detail-value">{revision}</span>
            </div>
            <div className="approval-modal__detail">
              <span className="approval-modal__detail-label">Plan Hash</span>
              <span className="approval-modal__detail-value mono">
                {truncHash(planHash)}
              </span>
            </div>
            <div className="approval-modal__detail">
              <span className="approval-modal__detail-label">Actions</span>
              <span className="approval-modal__detail-value">
                {actionCount}
              </span>
            </div>
            <div className="approval-modal__detail">
              <span className="approval-modal__detail-label">Risk</span>
              <span
                className="approval-modal__detail-value"
                style={{
                  color:
                    riskLevel === "high" || riskLevel === "critical"
                      ? "var(--red)"
                      : riskLevel === "medium"
                      ? "var(--amber)"
                      : "var(--emerald)",
                  fontWeight: 700,
                }}
              >
                {(riskLevel || "unknown").toUpperCase()}
              </span>
            </div>
          </div>
        </div>
      <div className="approval-modal__footer">
        <button
          className="approval-btn approval-btn--approve"
          onClick={onConfirm}
        >
            Authorize Execution
        </button>
        <button
            className="approval-btn approval-btn--cancel"
            onClick={onCancel}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

/* ── Main ApprovalTab ────────────────────────────────────────────────────── */
export default function ApprovalTab({
  contract,
  approvalToken,
  incidentId,
  incidentStatus,
  events,
  onDecisionSubmitted,
  incidentDetail,
}) {
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  const [timeLeft, setTimeLeft] = useState(null);

  const isActive = incidentStatus === "AWAITING_APPROVAL";
  const isTerminal = TERMINAL_STATUSES.includes(incidentStatus);

  // Contract expiry countdown — MUST be above all early returns (Rules of Hooks)
  const expiresAt = contract?.expires_at || contract?.token_expires_at || "";

  useEffect(() => {
    if (!expiresAt || incidentStatus !== "AWAITING_APPROVAL") {
      setTimeLeft(null);
      return;
    }
    function tick() {
      const diff = new Date(expiresAt).getTime() - Date.now();
      setTimeLeft(Math.max(0, Math.floor(diff / 1000)));
    }
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [expiresAt, incidentStatus]);

  // ── Terminal: delegate to ResolutionRecord ──
  if (isTerminal) {
    return (
      <ResolutionRecord
        events={events}
        incidentId={incidentId}
        incidentStatus={incidentStatus}
        incidentDetail={incidentDetail}
      />
    );
  }

  const isExpired = timeLeft !== null && timeLeft <= 0;
  const expiryDisplay =
    timeLeft !== null
      ? timeLeft > 60
        ? `${Math.floor(timeLeft / 60)}m ${timeLeft % 60}s`
        : `${timeLeft}s`
      : null;

  if (!isActive || !contract) {
    return (
      <div className="approval-tab">
        <div className="empty-state">
          <span className="empty-state__icon">[IDLE]</span>
          <span className="empty-state__text">
            {incidentStatus === "AWAITING_APPROVAL"
              ? "Loading contract…"
              : "No approval gate is active"}
          </span>
          <span className="empty-state__hint">
            {incidentStatus === "AWAITING_APPROVAL"
              ? "The approval contract is being loaded."
              : "The approval gate activates when the incident reaches AWAITING_APPROVAL. Trigger a live scenario to see an exact contract and approve/reject workflow."}
          </span>
          {incidentStatus && (
            <span className="empty-state__hint">
              Current status:{" "}
              <strong>{incidentStatus.replace(/_/g, " ")}</strong>
            </span>
          )}
        </div>
      </div>
    );
  }

  // ── Extract plan details from contract payload ──
  const plan = contract.plan || contract;
  const topActions = contract.actions || plan.actions || [];
  const riskLevel = plan.risk_level || "unknown";
  const mttr = plan.estimated_mttr_minutes || plan.mttr_minutes || "?";
  const contractId = contract.contract_id || contract.id || "";
  const planHash = contract.plan_hash || "";
  const revision = contract.revision ?? 1;

  // Strategy summary — from nested plan
  const strategySummary =
    plan.strategy_summary || plan.summary || plan.strategy || "";
  const cleanSummary = strategySummary.split("[Revision feedback:")[0].trim();

  // ── Council Recommendation from structured events ──
  const recommendation = deriveCouncilRecommendation(events || [], revision);

  // ── Incident context ──
  const svc = incidentDetail?.service_id || "";
  const sev = incidentDetail?.severity || "";
  const incSummary = incidentDetail?.summary || "";
  const incCreated = incidentDetail?.created_at || "";

  // ── Workflow steps from events (not room messages) ──
  const steps = [];
  if (recommendation.triage) {
    steps.push({
      label: "Triaged",
      agent: "nigehban",
      done: true,
    });
  }
  if (recommendation.investigation) {
    steps.push({
      label: "Investigated",
      agent: "muhaqqiq",
      done: true,
    });
  }
  if (recommendation.plan) {
    const planData = recommendation.plan.data;
    if (planData.revision > 1) {
      steps.push({ label: "Challenged", agent: "muhtasib", done: true });
      steps.push({
        label: `Revised (r${planData.revision})`,
        agent: "mudabbir",
        done: true,
      });
    } else {
      steps.push({ label: "Planned", agent: "mudabbir", done: true });
    }
  }
  if (recommendation.verdict) {
    const vp = recommendation.verdict.payload;
    const decision = vp.verdict || vp.decision || "";
    if (decision.includes("PASSED") || decision.includes("APPROVED")) {
      steps.push({
        label: "Safety-approved",
        agent: "muhtasib",
        done: true,
      });
    }
  }
  steps.push({
    label: "Awaiting operator",
    agent: "system",
    done: false,
    current: true,
  });

  const handleDecision = async (action) => {
    if (action === "APPROVE") {
      setShowConfirmModal(true);
      return;
    }
    if (action === "REQUEST_REVISION" && !showFeedback) {
      setShowFeedback(true);
      return;
    }
    await submitDecision(action);
  };

  const submitDecision = async (action) => {
    setSubmitting(true);
    setError(null);
    try {
      const body = {
        contract_id: contractId,
        revision,
        action,
        operator_label: "dashboard-operator",
      };
      if (action === "APPROVE") body.approval_token = approvalToken;
      if (action === "REQUEST_REVISION")
        body.feedback = feedback || "Please revise the plan.";
      await apiFetch(`/incidents/${incidentId}/decisions`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (onDecisionSubmitted) onDecisionSubmitted(action);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
      setShowConfirmModal(false);
    }
  };

  // ── Verdict detail for safety section ──
  const verdictPayload = recommendation.verdict?.payload || {};
  const verdictDecision =
    verdictPayload.verdict || verdictPayload.decision || "";
  const verdictReasoning = verdictPayload.reasoning || "";

  // Challenge count from events (look for verdicts with CHALLENGE)
  const allVerdicts = (events || []).filter(
    (e) => e.event_type === "verdict_issued"
  );
  const challengeCount = allVerdicts.filter((e) => {
    const vp = eventPayload(e);
    const d = (vp.verdict || vp.decision || "").toUpperCase().trim();
    return d === "CHALLENGE" || d === "CHALLENGED";
  }).length;

  return (
    <div
      className="approval-tab"
      style={{ display: "flex", flexDirection: "column", height: "100%" }}
    >
      {/* Scrollable content */}
      <div style={{ flex: 1, overflowY: "auto", paddingBottom: 80 }}>
        <div className="approval-tab__card">
          {/* Header */}
          <div className="approval-tab__header">
            <span className="approval-tab__header-icon">OPERATOR AUTHORIZATION REQUIRED</span>
            <span>Critical Safety Gate</span>
          </div>

          {/* Incident context bar */}
          {(svc || sev) && (
            <div className="approval-context-bar">
              {sev && (
                <span
                  className={`incident-item__severity severity--${sev}`}
                  style={{ fontSize: 10, padding: "2px 6px" }}
                >
                  {sev}
                </span>
              )}
              {svc && <span style={{ fontWeight: 600 }}>{svc}</span>}
              {incSummary && (
                <span style={{ color: "var(--text-muted)" }}>·</span>
              )}
              {incSummary && (
                <span style={{ color: "var(--text-secondary)" }}>
                  {incSummary}
                </span>
              )}
              {incCreated && (
                <span
                  style={{
                    color: "var(--text-dim)",
                    marginLeft: "auto",
                    flexShrink: 0,
                  }}
                >
                  {relativeTime(incCreated)}
                </span>
              )}
            </div>
          )}

          {/* Workflow strip — nowrap */}
          {steps.length > 0 && (
            <div
              className="approval-workflow-strip"
              style={{ flexWrap: "nowrap", overflowX: "auto" }}
            >
              {steps.map((step, i) => (
                <div key={i} className="approval-workflow-strip__item">
                  {i > 0 && (
                    <span className="approval-workflow-strip__arrow">{"->"}</span>
                  )}
                  <span
                    className={`approval-workflow-strip__step ${
                      step.current
                        ? "approval-workflow-strip__step--current"
                        : ""
                    }`}
                    style={{
                      color: step.done
                        ? AGENT_PERSONAS[step.agent]?.accent ||
                          "var(--text-secondary)"
                        : "var(--status-pending)",
                    }}
                    >
                    {step.done ? "DONE" : "PENDING"} {step.label}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Expired contract warning */}
          {isExpired && (
            <div className="approval-expired-banner">
              Contract Expired — Request a revised plan for revalidation.
            </div>
          )}

          {/* Two-column grid */}
          <div className="approval-grid">
            {/* Left column: strategy + contract details */}
            <div className="approval-grid__left">
              <div className="approval-tab__details">
                <div className="approval-tab__detail-row">
                  <span className="approval-tab__detail-label">Contract</span>
                  <span className="approval-tab__detail-value mono">
                    {contractId || "—"}
                  </span>
                </div>
                <div className="approval-tab__detail-row">
                  <span className="approval-tab__detail-label">Plan Hash</span>
                  <span className="approval-tab__detail-value mono">
                    {truncHash(planHash) || "—"}
                  </span>
                </div>
                <div className="approval-tab__detail-row">
                  <span className="approval-tab__detail-label">Revision</span>
                  <span className="approval-tab__detail-value">{revision}</span>
                </div>
                <div className="approval-tab__detail-row">
                  <span className="approval-tab__detail-label">Risk</span>
                  <span className="approval-tab__detail-value">
                    <strong
                      style={{
                        color:
                          riskLevel === "high" || riskLevel === "critical"
                            ? "var(--red)"
                            : riskLevel === "medium"
                            ? "var(--amber)"
                            : "var(--emerald)",
                      }}
                    >
                      {riskLevel.toUpperCase()}
                    </strong>
                  </span>
                </div>
                <div className="approval-tab__detail-row">
                  <span className="approval-tab__detail-label">Est. MTTR</span>
                  <span className="approval-tab__detail-value">
                    <strong>{mttr} min</strong>
                  </span>
                </div>
                <div className="approval-tab__detail-row">
                  <span className="approval-tab__detail-label">Expires</span>
                  <span
                    className="approval-tab__detail-value"
                    style={{
                      color: isExpired
                        ? "var(--red)"
                        : timeLeft !== null && timeLeft < 60
                        ? "var(--amber)"
                        : "var(--text-secondary)",
                      fontWeight: 600,
                    }}
                  >
                    {isExpired ? "EXPIRED" : expiryDisplay || "—"}
                  </span>
                </div>
              </div>

              {/* Strategy */}
              <div className="approval-tab__section">
                <div className="approval-tab__section-title">Strategy</div>
                <p className="approval-tab__plan-text">
                  {cleanSummary || strategySummary || "Loading plan summary…"}
                </p>
              </div>

              {/* Safety Review (moved to left for balance) */}
              <div className="approval-tab__section">
                <div className="approval-tab__section-title">Safety Review</div>
                <div className="approval-safety-summary">
                <div className="approval-safety-summary__status">
                    {verdictDecision.includes("PASSED") ||
                    verdictDecision.includes("APPROVED") ? (
                      <span style={{ color: "var(--emerald)" }}>PASSED</span>
                    ) : verdictDecision.includes("CHALLENGE") ? (
                      <span style={{ color: "var(--amber)" }}>
                        CHALLENGED
                      </span>
                    ) : (
                      <span style={{ color: "var(--text-muted)" }}>
                        Pending
                      </span>
                    )}
                    {challengeCount > 0 ? (
                      <span
                        style={{
                          color: "var(--text-secondary)",
                          fontSize: 12,
                        }}
                      >
                        {" "}
                        — {challengeCount} challenge
                        {challengeCount > 1 ? "s" : ""} {"->"}  revision {revision}{" "}
                        {"->"} Muhtasib approved
                      </span>
                    ) : (
                      verdictDecision && (
                        <span
                          style={{
                            color: "var(--text-secondary)",
                            fontSize: 12,
                          }}
                        >
                          {" "}
                          — Muhtasib approved on first review
                        </span>
                      )
                    )}
                  </div>
                  {verdictReasoning && (
                    <p className="approval-safety-summary__reason">
                      {verdictReasoning.substring(0, 300)}
                    </p>
                  )}
                </div>
              </div>

              {/* Policy Proof (moved to left for balance) */}
              <div className="approval-proof-card">
                <div className="approval-proof-card__title">Policy Proof</div>
                <div className="approval-tab__details">
                  <div className="approval-tab__detail-row">
                    <span className="approval-tab__detail-label">Skill Allowlist</span>
                    <span className="approval-tab__detail-value">
                      {topActions.length > 0 ? topActions.map(a => typeof a === 'object' ? (a.skill || a.name || a.skill_name || 'action') : a).join(', ') : 'Awaiting plan'}
                    </span>
                  </div>
                  <div className="approval-tab__detail-row">
                    <span className="approval-tab__detail-label">Service Scope</span>
                    <span className="approval-tab__detail-value">
                      {incidentDetail?.service_id || 'Awaiting plan'}
                    </span>
                  </div>
                  <div className="approval-tab__detail-row">
                    <span className="approval-tab__detail-label">Bounded Parameters</span>
                    <span className="approval-tab__detail-value">
                      {topActions.length > 0 ? `${topActions.length} actions bounded` : 'Awaiting plan'}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            {/* Right column: actions + integrity + outcome */}
            <div className="approval-grid__right">
              {/* Exact Actions */}
              {topActions.length > 0 && (
                <div className="approval-tab__section">
                  <div className="approval-tab__section-title">
                    Exact Actions ({topActions.length})
                  </div>
                  <div className="approval-actions-list" style={{ maxHeight: "40vh", overflowY: "auto" }}>
                    {topActions.map((action, i) => {
                      const isObj = typeof action === "object";
                      const skillName = isObj
                        ? action.skill ||
                          action.name ||
                          action.skill_name ||
                          "action"
                        : action;
                      const args = isObj
                        ? action.arguments || action.args || {}
                        : {};
                      const deps = isObj ? action.depends_on || [] : [];
                      const onFail = isObj ? action.on_failure || "" : "";
                      const target = isObj ? action.target || "" : "";

                      return (
                        <div key={i} className="approval-action-card">
                          <div className="approval-action-card__header">
                            <span
                              style={{
                                color: "var(--text-dim)",
                                fontSize: 12,
                              }}
                            >
                              {i + 1}.
                            </span>
                            <span className="skill-badge">{skillName}</span>
                            {target && (
                              <span
                                style={{
                                  fontSize: 11,
                                  color: "var(--text-muted)",
                                }}
                              >
                                to {target}
                              </span>
                            )}
                          </div>
                          {Object.keys(args).length > 0 && (
                            <div className="approval-action-card__args">
                              {Object.entries(args).map(([k, v]) => (
                                <div
                                  key={k}
                                  className="approval-action-card__arg"
                                >
                                  <span className="approval-action-card__arg-key">
                                    {k}
                                  </span>
                                  <span className="approval-action-card__arg-value">
                                    {typeof v === "object"
                                      ? JSON.stringify(v)
                                      : String(v)}
                                  </span>
                                </div>
                              ))}
                            </div>
                          )}
                          <div className="approval-action-card__meta">
                            {deps.length > 0 && (
                              <span
                                style={{
                                  fontSize: 10,
                                  color: "var(--text-dim)",
                                }}
                              >
                                depends: {deps.join(", ")}
                              </span>
                            )}
                            {onFail && (
                              <span
                                style={{
                                  fontSize: 10,
                                  color:
                                    onFail === "STOP"
                                      ? "var(--red)"
                                      : "var(--text-dim)",
                                }}
                              >
                                on_failure: {onFail}
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Integrity Proof */}
              <div className="approval-proof-card">
                <div className="approval-proof-card__title">Integrity Proof</div>
                <div className="approval-tab__details">
                  <div className="approval-tab__detail-row">
                    <span className="approval-tab__detail-label">Contract HMAC</span>
                    <span className="approval-tab__detail-value">
                      {approvalToken ? 'Present' : 'Not issued yet'}
                    </span>
                  </div>
                  <div className="approval-tab__detail-row">
                    <span className="approval-tab__detail-label">Nonce</span>
                    <span className="approval-tab__detail-value mono">
                      {contractId ? contractId.substring(0, 12) + '\u2026' : 'Not issued yet'}
                    </span>
                  </div>
                  <div className="approval-tab__detail-row">
                    <span className="approval-tab__detail-label">TTL</span>
                    <span className="approval-tab__detail-value" style={{ color: isExpired ? 'var(--red)' : timeLeft !== null && timeLeft < 60 ? 'var(--amber)' : 'var(--text-secondary)' }}>
                      {isExpired ? 'Expired' : expiryDisplay || 'Not issued yet'}
                    </span>
                  </div>
                  <div className="approval-tab__detail-row">
                    <span className="approval-tab__detail-label">Plan Hash</span>
                    <span className="approval-tab__detail-value mono">
                      {planHash ? truncHash(planHash) + '\u2026' : 'Not issued yet'}
                    </span>
                  </div>
                </div>
              </div>

              {/* Execution Outcome */}
              <div className="approval-proof-card">
                <div className="approval-proof-card__title">Execution Outcome</div>
                <div className="approval-tab__details">
                  <div className="approval-tab__detail-row">
                    <span className="approval-tab__detail-label">Status</span>
                    <span className="approval-tab__detail-value">Pending decision</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {error && (
            <div
              style={{ fontSize: 12, color: "var(--red)", padding: "8px 16px" }}
            >
              {error}
            </div>
          )}
        </div>
      </div>

      {/* ── Exact Contract Card (Item 14) ── */}
      <div className="exact-contract-card">
        <div className="exact-contract-card__title">EXACT CONTRACT</div>
        <div className="approval-tab__details">
          <div className="approval-tab__detail-row">
            <span className="approval-tab__detail-label">Contract</span>
            <span className="approval-tab__detail-value mono">{contractId || "—"}</span>
          </div>
          <div className="approval-tab__detail-row">
            <span className="approval-tab__detail-label">Revision</span>
            <span className="approval-tab__detail-value">{revision ?? "—"}</span>
          </div>
          <div className="approval-tab__detail-row">
            <span className="approval-tab__detail-label">Plan hash</span>
            <span className="approval-tab__detail-value mono">{truncHash(planHash)}…</span>
          </div>
          <div className="approval-tab__detail-row">
            <span className="approval-tab__detail-label">Actions</span>
            <span className="approval-tab__detail-value">{topActions.length}</span>
          </div>
          {expiresAt && (
            <div className="approval-tab__detail-row">
              <span className="approval-tab__detail-label">Expires</span>
              <span className="approval-tab__detail-value">{formatTime(expiresAt)}</span>
            </div>
          )}
        </div>
        <p className="exact-contract-card__notice">
          Approving authorizes only this exact plan. Any revision, hash, or token mismatch is rejected.
        </p>
      </div>

      {/* Sticky decision footer */}
      <div className="approval-sticky-footer">
        <div className="approval-sticky-footer__confirm">
          <span
            className="mono"
            style={{ fontSize: 11, color: "var(--text-dim)" }}
                    >
            {contractId} · rev {revision} · {truncHash(planHash)} ·{" "}
            {topActions.length} action{topActions.length !== 1 ? "s" : ""}
          </span>
          {isExpired && (
            <span
              style={{ color: "var(--red)", fontSize: 11, fontWeight: 600 }}
            >
              Contract Expired — Request a revised plan for revalidation.
            </span>
          )}
        </div>
        {showFeedback && (
          <textarea
            className="approval-tab__feedback-area"
            placeholder="Describe what should be revised…"
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            autoFocus
          />
        )}
        <div className="approval-sticky-footer__buttons">
          <button
            className="approval-btn approval-btn--approve"
            onClick={() => handleDecision("APPROVE")}
            disabled={submitting || isExpired}
          >
            {submitting ? "Submitting…" : "Authorize Execution"}
          </button>
          <button
            className="approval-btn approval-btn--revise"
            onClick={() => handleDecision("REQUEST_REVISION")}
            disabled={submitting}
          >
            {showFeedback ? "Send Revision" : "Request Revision"}
          </button>
          <button
            className="approval-btn approval-btn--reject"
            onClick={() => handleDecision("REJECT")}
            disabled={submitting}
          >
            Deny Execution
          </button>
        </div>
      </div>

      {/* Confirmation Modal */}
      {showConfirmModal && (
        <ConfirmationModal
          contractId={contractId}
          revision={revision}
          planHash={planHash}
          actionCount={topActions.length}
          riskLevel={riskLevel}
          onConfirm={() => submitDecision("APPROVE")}
          onCancel={() => setShowConfirmModal(false)}
        />
      )}
    </div>
  );
}
