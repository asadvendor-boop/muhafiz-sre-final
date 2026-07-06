"use client";
/* ═══════════════════════════════════════════════════════════════════════════════
 * OverviewPage — Streamlined front page
 * ═══════════════════════════════════════════════════════════════════════════════
 * Hero + tech tags + council showcase + thesis + single CTA + disclosure.
 * No proof metrics, incident launcher, docket, evalData, or SCENARIOS.
 * ═══════════════════════════════════════════════════════════════════════════════ */

import { useState, useEffect, useRef } from "react";
import { AGENT_PERSONAS } from "../personas";

/* ── Agent Detail Drawer ─────────────────────────────────────────────────── */
function AgentDrawer({ agent, onClose }) {
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
      if (e.key === "Escape") { onCloseRef.current(); return; }
      if (e.key === "Tab" && drawerRef.current) {
        const focusable = drawerRef.current.querySelectorAll(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }
    }
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("keydown", handleKey);
      if (prev) prev.focus();
    };
  }, []);

  if (!agent) return null;

  const tierClass = agent.modelTier ? `agent-drawer__model-tier--${agent.modelTier}` : "";

  return (
    <>
      <div className="agent-drawer-overlay" onClick={onClose} />
      <div className="agent-drawer" ref={drawerRef}
           style={{ "--agent-accent": agent.accent }}
           role="dialog" aria-modal="true" aria-label={`${agent.displayName} details`}>
        <button className="agent-drawer__close" onClick={onClose}>Close</button>
        <img
          src={agent.portrait}
          alt={agent.displayName}
          className="agent-drawer__portrait"
          onError={(e) => { e.currentTarget.onerror = null; e.currentTarget.src = agent.fallbackAvatar; }}
        />
        <div>
          <span className="agent-drawer__name">{agent.displayName}</span>
          {agent.nativeName && (
            <span className="agent-drawer__native">({agent.nativeName})</span>
          )}
        </div>
        <div className="agent-drawer__role" style={{ color: agent.accent }}>{agent.role}</div>

        {agent.duty && (
          <>
            <div className="agent-drawer__section-title">Duty</div>
            <p className="agent-drawer__duty">{agent.duty}</p>
          </>
        )}

        {agent.model && (
          <>
            <div className="agent-drawer__section-title">Gemini Model</div>
            <div className="agent-drawer__model-badge">
              {agent.model}
              {agent.modelTier && (
                <span className={`agent-drawer__model-tier ${tierClass}`}>
                  {agent.modelTier}
                </span>
              )}
            </div>
          </>
        )}

        {agent.traits && agent.traits.length > 0 && (
          <>
            <div className="agent-drawer__section-title">Capabilities</div>
            <div className="agent-drawer__traits">
              {agent.traits.map((t) => (
                <span key={t} className="agent-drawer__trait">{t}</span>
              ))}
            </div>
          </>
        )}

        {agent.stages && agent.stages.length > 0 && (
          <>
            <div className="agent-drawer__section-title">Pipeline Stages</div>
            <div className="agent-drawer__stages"
                 style={{ "--agent-accent-soft": agent.accent ? agent.accent.replace(")", ", 0.15)").replace("rgb", "rgba") : undefined }}>
              {agent.stages.map((s) => (
                <span key={s} className="agent-drawer__stage">{s}</span>
              ))}
            </div>
          </>
        )}

        <div className="agent-drawer__section-title">Guiding Principle</div>
        <div className="agent-drawer__motto" style={{ borderColor: agent.accent }}>
          "{agent.motto}"
        </div>
      </div>
    </>
  );
}

/* ── Main OverviewPage ────────────────────────────────────────────────────── */
export default function OverviewPage({ onNavigate }) {
  const [drawerAgent, setDrawerAgent] = useState(null);

  return (
    <div className="welcome-panel">
      {/* ── 1. Hero Top: Logo → Title ── */}
      <div className="welcome-panel__hero-top">
        <img
          src="/muhafiz-logo.jpg"
          alt="MuhafizSRE"
          className="welcome-panel__hero-logo"
        />
        <h1 className="welcome-panel__hero-title">MuhafizSRE</h1>
        <p className="welcome-panel__hero-subtitle">Autonomous incident response, governed by operator authority.</p>
      </div>

      {/* ── 2. Tech Tags Row ── */}
      <div className="welcome-panel__tech-tags">
        <span className="tech-tag tech-tag--azure">ADK Multi-agent</span>
        <span className="tech-tag tech-tag--violet">MCP Telemetry</span>
        <span className="tech-tag tech-tag--emerald">Gemini 3-tier</span>
        <span className="tech-tag tech-tag--amber">HMAC Approval</span>
        <span className="tech-tag tech-tag--teal">Hash-Chain Audit</span>
        <span className="tech-tag tech-tag--gray">Cloud Run Live</span>
      </div>

      {/* ── 3. The Incident Council — Clickable Agent Cards ── */}
      <div className="council-showcase">
        <h3 className="council-showcase__heading">THE INCIDENT COUNCIL</h3>
        <div className="council-showcase__grid">
          {Object.values(AGENT_PERSONAS).map((agent) => (
            <div
              key={agent.id}
              className="council-card"
              style={{"--agent-accent": agent.accent}}
              onClick={() => setDrawerAgent(agent)}
              role="button"
              tabIndex={0}
              aria-label={`View details for ${agent.displayName}`}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setDrawerAgent(agent); } }}
            >
              <span className="council-card__badge">AI AGENT</span>
              <div className="council-card__portrait-wrap">
                <img
                  src={agent.portrait}
                  alt={agent.displayName}
                  className="council-card__portrait"
                  onError={(e) => { e.currentTarget.onerror = null; e.currentTarget.src = agent.fallbackAvatar; }}
                />
              </div>
              <div className="council-card__info">
                <div className="council-card__name">
                  <span className="council-card__icon">{agent.icon}</span>
                  {agent.displayName}
                </div>
                <div className="council-card__role">{agent.role}</div>
                <div className="council-card__motto">"{agent.motto}"</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── 4. Thesis ── */}
      <p className="council-showcase__description">
        Five specialized AI agents investigate, challenge, and prepare exact remediation contracts.<br />
        Nothing executes until an operator authorizes the signed plan.
      </p>

      {/* ── Primary CTA ── */}
      <button
        className="incident-launcher__primary"
        style={{ maxWidth: 480, margin: "0 auto" }}
        onClick={() => onNavigate("launch")}
      >
        ▶ Start Guided Incident
      </button>

      {/* ── Disclosure ── */}
      <div className="welcome-panel__disclosure">
        All agent identities shown are AI systems with live Gemini agent workflow
        and deterministic synthetic enterprise telemetry.
        Every remediation action requires explicit operator approval before execution.
      </div>

      {/* ── Agent Detail Drawer ── */}
      {drawerAgent && (
        <AgentDrawer agent={drawerAgent} onClose={() => setDrawerAgent(null)} />
      )}
    </div>
  );
}
