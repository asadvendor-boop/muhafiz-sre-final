"use client";
/* ═══════════════════════════════════════════════════════════════════════════════
 * CouncilPresenceBar — Compact agent presence strip
 * ═══════════════════════════════════════════════════════════════════════════════
 * Shows 34–40px avatar cards for all five agents.
 * Uses deriveAgentState() for lifecycle state, ordered by AGENT_ORDER.
 * onError falls back to abstract avatars.
 * Terminal → all participants show completed ✓.
 * ═══════════════════════════════════════════════════════════════════════════════ */

import { AGENT_PERSONAS, AGENT_ORDER } from "../personas";
import { deriveAgentState } from "../lib/eventSelectors";

export default function CouncilPresenceBar({ incidentStatus, events }) {
  return (
    <div className="council-presence-bar" role="status" aria-label="Agent presence">
      {AGENT_ORDER.map((agentId) => {
        const persona = AGENT_PERSONAS[agentId];
        const state = deriveAgentState(agentId, events || [], incidentStatus);

        return (
          <div
            key={agentId}
            className={`council-presence council-presence--${state}`}
            title={`${persona.displayName} — ${persona.shortRole}: ${state}`}
          >
            <img
              className="council-presence__avatar"
              src={persona.avatar}
              alt={`AI Agent: ${persona.displayName}`}
              style={{ "--agent-accent": persona.accent }}
              onError={(e) => {
                e.currentTarget.onerror = null;
                e.currentTarget.src = persona.fallbackAvatar;
              }}
            />
            <div className="council-presence__info">
              <span
                className="council-presence__name"
                style={{ color: persona.accent }}
              >
                {persona.displayName}
              </span>
              <span className="council-presence__role">
                {persona.shortRole}
              </span>
            </div>
            <span
              className={`council-presence__dot council-presence__dot--${state}`}
              style={{
                background:
                  state === "active"
                    ? persona.accent
                    : state === "completed"
                    ? "var(--emerald)"
                    : state === "challenged"
                    ? "var(--amber)"
                    : "var(--text-dim)",
              }}
            />
          </div>
        );
      })}
    </div>
  );
}
