"use client";
/* ═══════════════════════════════════════════════════════════════════════════════
 * AuditTab — Hash chain + event timeline + telemetry filters
 * ═══════════════════════════════════════════════════════════════════════════════
 * Filters: Workflow (default) | Security | Telemetry | All
 * Telemetry hidden under Workflow view.
 * Raw JSON in focus-trapped drawer.
 * Chain verification with 4 states (unchecked → verifying → verified | failed).
 * Uses chain_valid key from verification endpoint.
 * ═══════════════════════════════════════════════════════════════════════════════ */

import { useState, useEffect, useRef, useCallback } from "react";
import { EVENT_ICONS } from "../personas";
import { extractTelemetry, eventPayload } from "../lib/eventSelectors";

const API_BASE = "/api";

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

const FILTER_TABS = [
  { id: "workflow", label: "Workflow" },
  { id: "security", label: "Security" },
  { id: "telemetry", label: "Telemetry" },
  { id: "all", label: "All" },
];

const WORKFLOW_TYPES = [
  "incident_created", "triage_completed", "investigation_completed",
  "plan_created", "verdict_issued", "contract_issued",
  "human_approved", "human_rejected", "plan_validated",
  "actions_executed", "execution_started", "execution_completed",
  "recovery_verified", "seal", "outcome",
  "pipeline_failed", "execution_failed", "recovery_failed",
];

const SECURITY_TYPES = [
  "verdict_issued", "contract_issued",
  "human_approved", "human_rejected", "plan_validated",
  "challenge_limit_reached",
  "pipeline_failed", "execution_failed", "recovery_failed",
  "seal",
];

const EVENT_DISPLAY_NAMES = {
  human_approved: "operator approved",
  human_rejected: "operator rejected",
};

/* ── JSON Detail Drawer with focus trap ──────────────────────────────────── */
function JsonDrawer({ data, onClose }) {
  const drawerRef = useRef(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const prev = document.activeElement;
    if (drawerRef.current) {
      const btn = drawerRef.current.querySelector("button");
      if (btn) btn.focus();
    }
    function handleKey(e) {
      if (e.key === "Escape") {
        onCloseRef.current();
        return;
      }
      // Focus trap: Tab wraps within drawer
      if (e.key === "Tab" && drawerRef.current) {
        const focusable = drawerRef.current.querySelectorAll(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey) {
          if (document.activeElement === first) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    }
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("keydown", handleKey);
      if (prev) prev.focus();
    };
  }, []);

  return (
    <div
      className="audit-json-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-label="Event JSON detail"
    >
      <div className="audit-json-drawer" ref={drawerRef}>
        <div className="audit-json-drawer__header">
          <span>Event Detail</span>
          <button
            className="audit-json-drawer__close"
            onClick={onClose}
            aria-label="Close"
          >
            CLOSE
          </button>
        </div>
        <pre className="audit-json-drawer__content">
          {JSON.stringify(data, null, 2)}
        </pre>
      </div>
    </div>
  );
}

export default function AuditTab({ events, incidentId }) {
  const [chainStatus, setChainStatus] = useState("unchecked");
  const [chainResult, setChainResult] = useState(null);
  const [verifiedAt, setVerifiedAt] = useState(null);
  const [activeFilter, setActiveFilter] = useState("workflow");
  const [jsonDrawerData, setJsonDrawerData] = useState(null);
  const verifyControllerRef = useRef(null);
  const verifyIncidentRef = useRef(incidentId);

  // Reset chain status on incident switch
  useEffect(() => {
    // Abort any in-flight verification from the previous incident
    if (verifyControllerRef.current) {
      verifyControllerRef.current.abort();
      verifyControllerRef.current = null;
    }
    verifyIncidentRef.current = incidentId;
    setChainStatus("unchecked");
    setChainResult(null);
    setVerifiedAt(null);
  }, [incidentId]);

  const handleVerify = useCallback(async () => {
    if (!incidentId) return;
    // Abort any prior in-flight verification
    if (verifyControllerRef.current) {
      verifyControllerRef.current.abort();
    }
    const controller = new AbortController();
    verifyControllerRef.current = controller;
    const targetId = incidentId;

    setChainStatus("verifying");
    try {
      const res = await fetch(
        `${API_BASE}/incidents/${targetId}/chain/verify`,
        { signal: controller.signal }
      );
      const result = await res.json();
      // Guard: discard if incident switched during the fetch
      if (verifyIncidentRef.current !== targetId) return;
      setChainResult(result);
      setChainStatus(result.chain_valid ? "verified" : "failed");
      setVerifiedAt(new Date().toISOString());
    } catch (err) {
      if (err.name === "AbortError") return;
      if (verifyIncidentRef.current !== targetId) return;
      setChainStatus("failed");
    }
  }, [incidentId]);

  // ── Filtered events ──
  const allEvents = events || [];
  const telemetryEvents = extractTelemetry(allEvents);

  const filteredEvents = allEvents.filter((evt) => {
    const type = evt.event_type || "";
    switch (activeFilter) {
      case "workflow":
        return WORKFLOW_TYPES.includes(type);
      case "security":
        return SECURITY_TYPES.includes(type);
      case "telemetry":
        return type === "agent_usage_telemetry";
      case "all":
      default:
        return true;
    }
  });

  // Chain events for visualization
  const chainEvents = allEvents.filter(
    (e) => e.event_hash || e.sequence !== undefined
  );

  // Derive genesis, head, seal from events
  const sortedChain = [...allEvents].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
  const genesisHash = sortedChain[0]?.previous_hash || sortedChain[0]?.event_hash;
  const headHash = sortedChain.at(-1)?.event_hash;
  const sealEvent = sortedChain.find((e) => e.event_type === "seal");

  // Derive executive summary data
  const totalEvents = allEvents.length;
  const uniqueAgents = new Set(
    allEvents.map((e) => e.actor || "").filter(Boolean)
  );
  const lastEventTime = sortedChain.at(-1)?.timestamp || sortedChain.at(-1)?.created_at;
  // Normalize tool-count fields: arrays → .length, numbers → value, missing → 0
  const countTools = (v) => {
    if (Array.isArray(v)) return v.length;
    if (typeof v === "number") return v;
    return 0;
  };
  const totalToolCalls = telemetryEvents.reduce((sum, evt) => {
    const p = eventPayload(evt);
    return sum + countTools(p.total_tool_calls ?? p.tools_succeeded);
  }, 0);
  const totalTokens = telemetryEvents.reduce((sum, evt) => {
    const p = eventPayload(evt);
    return sum + (p.total_tokens ?? 0);
  }, 0);

  return (
    <div className="audit-tab">
      {/* ── Chain Proof Panel ── */}
      <div className={`chain-proof-panel ${chainStatus === 'verified' ? 'chain-proof-panel--valid' : chainStatus === 'failed' ? 'chain-proof-panel--invalid' : 'chain-proof-panel--unchecked'}`}>
        <div className="chain-proof-panel__title">Chain Integrity</div>
        <div className="chain-proof-panel__grid">
          <div className="chain-proof-panel__field">
            <span className="chain-proof-panel__label">Genesis Hash</span>
            <span className="chain-proof-panel__value mono">{genesisHash ? truncHash(genesisHash) : 'Pending'}</span>
          </div>
          <div className="chain-proof-panel__field">
            <span className="chain-proof-panel__label">Head Hash</span>
            <span className="chain-proof-panel__value mono">{headHash ? truncHash(headHash) : 'Pending'}</span>
          </div>
          <div className="chain-proof-panel__field">
            <span className="chain-proof-panel__label">Chain Length</span>
            <span className="chain-proof-panel__value">{sortedChain.length} events</span>
          </div>
          <div className="chain-proof-panel__field">
            <span className="chain-proof-panel__label">Seal Status</span>
            <span className="chain-proof-panel__value">{sealEvent ? 'Sealed' : 'Pending'}</span>
          </div>
          <div className="chain-proof-panel__field">
            <span className="chain-proof-panel__label">Verification</span>
            <span className="chain-proof-panel__value">
              {chainStatus === 'verified' ? 'Chain intact' : chainStatus === 'failed' ? 'Chain broken' : 'Awaiting verification'}
            </span>
          </div>
          <div className="chain-proof-panel__field">
            <span className="chain-proof-panel__label">Tamper Check</span>
            <span className="chain-proof-panel__value">
              {chainStatus === 'verified' ? 'No tampering' : chainStatus === 'failed' ? 'Tampering detected' : 'Pending'}
            </span>
          </div>
        </div>
      </div>

      {/* ── Audit Executive Summary (Item 15) ── */}
      <div className="audit-exec-summary">
        <div className="audit-exec-summary__stat">
          <span className="audit-exec-summary__value">{totalEvents}</span>
          <span className="audit-exec-summary__label">Events</span>
        </div>
        <div className="audit-exec-summary__stat">
          <span className="audit-exec-summary__value">{chainEvents.length}</span>
          <span className="audit-exec-summary__label">Chain links</span>
        </div>
        <div className="audit-exec-summary__stat">
          <span className="audit-exec-summary__value">{uniqueAgents.size}</span>
          <span className="audit-exec-summary__label">Agents</span>
        </div>
        <div className="audit-exec-summary__stat">
          <span className="audit-exec-summary__value">{sealEvent ? "SEALED" : "—"}</span>
          <span className="audit-exec-summary__label">Seal</span>
        </div>
        {totalToolCalls > 0 && (
          <div className="audit-exec-summary__stat">
            <span className="audit-exec-summary__value">{totalToolCalls}</span>
            <span className="audit-exec-summary__label">Tool calls</span>
          </div>
        )}
        {totalTokens > 0 && (
          <div className="audit-exec-summary__stat">
            <span className="audit-exec-summary__value">{Math.round(totalTokens / 1000)}k</span>
            <span className="audit-exec-summary__label">Tokens</span>
          </div>
        )}
        {lastEventTime && (
          <div className="audit-exec-summary__stat">
            <span className="audit-exec-summary__value">{formatTime(lastEventTime)}</span>
            <span className="audit-exec-summary__label">Last event</span>
          </div>
        )}
      </div>

      {/* Hash Chain Section */}
      <div className="audit-tab__chain-section">
        <div className="audit-tab__chain-header">
          <div className="audit-tab__chain-title">
            <span>SHA-256 Hash Chain</span>
            {chainStatus === "verified" && (
              <span className="chain-status chain-status--valid">Chain Intact</span>
            )}
            {chainStatus === "failed" && (
              <span className="chain-status chain-status--invalid">Chain Broken</span>
            )}
            {chainStatus === "unchecked" && (
              <span
                className="chain-status"
                style={{ color: "var(--text-dim)" }}
              >
                Awaiting Verification
              </span>
            )}
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              className={`audit-tab__verify-btn ${
                chainStatus === "failed" ? "audit-tab__verify-btn--invalid" : ""
              }`}
              onClick={handleVerify}
              disabled={chainStatus === "verifying"}
            >
              {chainStatus === "verifying" ? (
                <span
                  style={{ display: "flex", alignItems: "center", gap: 6 }}
                >
                  <span className="spinner" /> Verifying…
                </span>
              ) : chainStatus === "verified" ? (
                "Re-verify"
              ) : (
                "Verify Chain"
              )}
            </button>
            {chainStatus === "verified" && (
              <button
                className="audit-tab__verify-btn"
                onClick={() => {
                  const proof = {
                    incident_id: incidentId,
                    genesis: genesisHash,
                    head: headHash,
                    seal: sealEvent?.event_hash || null,
                    events: sortedChain.length,
                    chain_valid: chainResult?.chain_valid ?? true,
                    verified_at: verifiedAt,
                  };
                  navigator.clipboard?.writeText(JSON.stringify(proof, null, 2));
                }}
                aria-label="Copy verification proof"
              >
                Copy Proof
              </button>
            )}
          </div>
        </div>
        {/* Genesis / Head / Seal hashes */}
        <div className="audit-chain-summary">
          <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
            Genesis: {truncHash(genesisHash)}
          </span>
          <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
            Head: {truncHash(headHash)}
          </span>
          <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
            Events: {sortedChain.length}
          </span>
          {sealEvent && (
            <span style={{ fontSize: 12, color: "var(--emerald)" }}>
              Sealed
            </span>
          )}
          {verifiedAt && (
            <span style={{ fontSize: 11, color: "var(--text-dim)" }}>
              Checked: {formatTime(verifiedAt)}
            </span>
          )}
        </div>

        {chainEvents.length > 0 ? (
          <div className="audit-chain">
            {chainEvents.map((evt, i) => (
              <div
                key={evt.event_id || i}
                style={{ display: "flex", alignItems: "center", gap: 6 }}
              >
                {i > 0 && (
                  <span className="audit-chain__connector">-&gt;</span>
                )}
                <div className="audit-chain__entry">
                  <span className="audit-chain__seq">
                    {evt.sequence ?? i}
                  </span>
                  <span className="audit-chain__hash">
                    {truncHash(evt.event_hash)}
                  </span>
                  <span className="audit-chain__type">
                    {EVENT_DISPLAY_NAMES[evt.event_type] || (evt.event_type || "").replace(/_/g, " ")}
                  </span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div
            style={{
              color: "var(--text-dim)",
              fontSize: 13,
              padding: "8px 0",
            }}
          >
            No chain events yet.
          </div>
        )}
      </div>

      {/* Filter bar */}
      <div
        className="audit-filter-bar"
        role="tablist"
        onKeyDown={(e) => {
          const tabs = FILTER_TABS.map((t) => t.id);
          const idx = tabs.indexOf(activeFilter);
          let next = idx;
          if (e.key === "ArrowRight" || e.key === "ArrowDown") {
            next = (idx + 1) % tabs.length;
          } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
            next = (idx - 1 + tabs.length) % tabs.length;
          } else if (e.key === "Home") {
            next = 0;
          } else if (e.key === "End") {
            next = tabs.length - 1;
          } else {
            return;
          }
          e.preventDefault();
          setActiveFilter(tabs[next]);
          e.currentTarget.querySelectorAll('[role="tab"]')[next]?.focus();
        }}
      >
        {FILTER_TABS.map((tab) => (
          <button
            key={tab.id}
            className={`audit-filter-bar__tab ${
              activeFilter === tab.id ? "audit-filter-bar__tab--active" : ""
            }`}
            onClick={() => setActiveFilter(tab.id)}
            role="tab"
            aria-selected={activeFilter === tab.id}
            tabIndex={activeFilter === tab.id ? 0 : -1}
          >
            {tab.label}
            {tab.id === "telemetry" && telemetryEvents.length > 0 && (
              <span className="audit-filter-bar__count">
                {telemetryEvents.length}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Telemetry cards (only in telemetry view) */}
      {activeFilter === "telemetry" && telemetryEvents.length > 0 && (
        <div className="audit-telemetry-cards">
          {telemetryEvents.map((evt, i) => {
            const tp = eventPayload(evt);
            return (
              <div key={evt.event_id || `telem-${i}`} className="audit-telemetry-card">
                <div className="audit-telemetry-card__header">
                  <span style={{ fontWeight: 600, fontSize: 13 }}>
                    {tp.agent || evt.actor || "Agent"}
                  </span>
                  <span style={{ fontSize: 11, color: "var(--text-dim)" }}>
                    {formatTime(evt.timestamp)}
                  </span>
                </div>
                <div className="audit-telemetry-card__body">
                  {tp.prompt_tokens != null && (
                    <span>Prompt: {tp.prompt_tokens.toLocaleString()}</span>
                  )}
                  {tp.candidates_tokens != null && (
                    <span>Output: {tp.candidates_tokens.toLocaleString()}</span>
                  )}
                  {tp.thoughts_tokens != null && (
                    <span>Thinking: {tp.thoughts_tokens.toLocaleString()}</span>
                  )}
                  {tp.total_tokens != null && (
                    <span>Total: {tp.total_tokens.toLocaleString()}</span>
                  )}
                  {tp.thinking_level && (
                    <span>
                      Level: {String(tp.thinking_level).replace("ThinkingLevel.", "")}
                    </span>
                  )}
                  {Array.isArray(tp.tools_called) && (
                    <span>
                      Tools: {tp.tools_succeeded?.length || 0}/{tp.tools_called.length} succeeded
                    </span>
                  )}
                  {tp.tools_failed?.length > 0 && (
                    <span style={{ color: "var(--red)" }}>
                      Failed: {tp.tools_failed.join(", ")}
                    </span>
                  )}
                  {tp.model && <span>Model: {tp.model}</span>}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Event Timeline */}
      <div className="audit-tab__timeline-section">
        <div className="audit-tab__timeline-title">
            <span>Event Timeline</span>
          <span className="event-count">{filteredEvents.length}</span>
        </div>

        {filteredEvents.length === 0 ? (
          <div
            style={{
              color: "var(--text-dim)",
              fontSize: 13,
              padding: "16px 0",
              textAlign: "center",
            }}
          >
            No events match this filter.
          </div>
        ) : (
          <div className="audit-timeline">
            {filteredEvents.map((evt, i) => {
              const type = evt.event_type || "unknown";
              const icon = EVENT_ICONS[type] || "#";

              return (
                <div key={evt.event_id || i} className="audit-event">
                  <div className="audit-event__header">
                    <span className={`audit-event__dot event-dot--${type}`}>
                      {icon}
                    </span>
                    <span
                      className={`audit-event__type event-color--${type}`}
                    >
                      {EVENT_DISPLAY_NAMES[type] || type.replace(/_/g, " ")}
                    </span>
                    {evt.actor && (
                      <span className="audit-event__actor">
                        by <strong>{evt.actor}</strong>
                      </span>
                    )}
                    <span className="audit-event__time">
                      {formatTime(evt.timestamp)}
                    </span>
                    {(evt.event_hash || evt.previous_hash) && (
                      <span className="audit-event__hashes">
                        {evt.previous_hash && (
                          <>
                            <span className="hash-value">
                              {truncHash(evt.previous_hash)}
                            </span>
                            <span className="hash-arrow">-&gt;</span>
                          </>
                        )}
                        <span
                          className="hash-value"
                          style={{ color: "var(--cyan)" }}
                        >
                          {truncHash(evt.event_hash)}
                        </span>
                      </span>
                    )}
                    <button
                      className="audit-event__expand-btn"
                      onClick={() => setJsonDrawerData(evt)}
                      aria-label={`View details for ${type}`}
                    >
                      Expand
                    </button>
                  </div>
                  {evt.summary && (
                    <div className="audit-event__summary">
                      {evt.summary}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* JSON drawer */}
      {jsonDrawerData && (
        <JsonDrawer
          data={jsonDrawerData}
          onClose={() => setJsonDrawerData(null)}
        />
      )}
    </div>
  );
}
