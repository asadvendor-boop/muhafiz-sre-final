"use client";
/* ═══════════════════════════════════════════════════════════════════════════════
 * AgentRoom — Curated room messages + authority boundary
 * ═══════════════════════════════════════════════════════════════════════════════
 * Guardrail: Renders ONLY curated room_message records from the five agents.
 * No event JSON, no telemetry, no gateway notices. Audit owns structured events.
 *
 * Authority boundary: derived from contract_issued event, rendered separately.
 * Agent activation banners: static UI labels from incidentStatus + persona data.
 *
 * v4 UI Polish: Stronger header bars, challenge spotlight, structured evidence
 * chips (from event payloads ONLY, no regex), status-driven activity strip.
 * ═══════════════════════════════════════════════════════════════════════════════ */

import { useEffect, useRef, useMemo } from "react";
import { AGENT_PERSONAS, MESSAGE_TYPE_LABELS, TERMINAL_STATUSES } from "../personas";
import { filterAgentRoomMessages, eventPayload } from "../lib/eventSelectors";
import CouncilPresenceBar from "./CouncilPresenceBar";

/* ── Role labels for header bars ─────────────────────────────────────────── */
const AGENT_ROLES = {
  nigehban:  "TRIAGE",
  muhaqqiq:  "INVESTIGATION",
  mudabbir:  "PLANNING",
  muhtasib:  "SAFETY REVIEW",
  aamil:     "EXECUTION",
};

/* ── Activity strip messages (status-driven, item 9) ─────────────────────── */
const ACTIVITY_MESSAGES = {
  DETECTED:   { agent: "nigehban", text: "Nigehban is triaging the alert…" },
  ANALYZING:  { agent: "muhaqqiq", text: "Muhaqqiq is collecting evidence…" },
  PLANNING:   { agent: "mudabbir", text: "Mudabbir is drafting a bounded remediation plan…" },
  REVIEWING:  { agent: "muhtasib", text: "Muhtasib is reviewing blast radius and policy compliance…" },
  EXECUTING:  { agent: "aamil",    text: "Aamil is executing the approved contract…" },
};

/** Render text with @mentions highlighted in per-agent color. No dangerouslySetInnerHTML. */
function renderMentions(text) {
  if (!text) return null;
  const parts = text.split(/(@\w+)/g);
  return parts.map((part, i) => {
    if (part.startsWith("@")) {
      const name = part.slice(1).toLowerCase();
      const persona = AGENT_PERSONAS[name];
      const color = persona ? persona.accent : "#94A3B8";
      return (
        <span
          key={i}
          className="mention-highlight"
          style={{ color, borderColor: color }}
        >
          {part}
        </span>
      );
    }
    // Pull leading punctuation flush against preceding mention chip
    const trimmed = i > 0 && parts[i - 1]?.startsWith("@")
      ? part.replace(/^\s+(?=[,.:;!?])/, "")
      : part;
    return <span key={i}>{trimmed}</span>;
  });
}

/** Resolve message type from msg_type or content heuristics */
function resolveMessageType(msg) {
  const mtype = msg.msg_type || msg.message_type || "";
  const sender = String(msg.sender || msg.actor || "").toLowerCase();
  const content = String(msg.content || "").toLowerCase();

  // Aamil recovery messages may arrive as "analysis" from the backend
  if (
    sender === "aamil" &&
    (mtype === "analysis" || mtype === "") &&
    /recovery|recovered|health check/i.test(content)
  ) {
    return "recovery";
  }

  // Muhtasib verdict normalization: backend stores all as "verdict"
  // but challenge/approval/blocked/escalated need distinct styling
  if (sender === "muhtasib" && mtype === "verdict") {
    if (/\bCHALLENGE\b/i.test(content)) return "challenge";
    if (/\bPASSED\b|APPROVED/i.test(content)) return "safety-approved";
    if (/\bBLOCKED\b/i.test(content)) return "blocked";
    if (/\bESCALATED\b/i.test(content)) return "escalated";
  }

  if (MESSAGE_TYPE_LABELS[mtype]) return mtype;
  return "system";
}

/** Compare events by sequence for ordering */
function compareEventSequence(a, b) {
  return (a.sequence ?? 0) - (b.sequence ?? 0);
}

/* ── Structured Evidence Chips (Item 8) ──────────────────────────────────── */
/* Derive ONLY from structured event payloads, NEVER from message text */

function deriveEvidenceChips(agentId, events) {
  const chips = [];
  const ordered = [...(events || [])].sort(compareEventSequence);

  if (agentId === "muhaqqiq") {
    const inv = [...ordered]
      .reverse()
      .find((e) => e.event_type === "investigation_completed");
    if (inv) {
      const p = eventPayload(inv);
      const sources = p.distinct_evidence_sources || p.evidence_sources || [];
      const evidence = Array.isArray(p.evidence) ? p.evidence : [];
      const sourceNames = new Set([
        ...sources,
        ...evidence.map((e) => e.source).filter(Boolean),
      ]);
      if (sourceNames.has("get_cloud_logging_traces") || sourceNames.has("cloud_logging") || sourceNames.has("logs"))
        chips.push({ label: "Logs", type: "good" });
      if (sourceNames.has("get_system_metrics") || sourceNames.has("metrics") || sourceNames.has("system_metrics"))
        chips.push({ label: "Metrics", type: "good" });
      if (sourceNames.has("get_github_deployments") || sourceNames.has("deployments") || sourceNames.has("github_deployments"))
        chips.push({ label: "Deployments", type: "good" });
      // Fallback: if sources exist but didn't match known names, show count
      if (chips.length === 0 && (sourceNames.size > 0 || evidence.length > 0)) {
        chips.push({ label: `${sourceNames.size || evidence.length} sources`, type: "good" });
      }
    }
    // Telemetry tool count — array-safe
    const tel = ordered.find(
      (e) => e.event_type === "agent_usage_telemetry" && (eventPayload(e).agent || "").toLowerCase() === "muhaqqiq"
    );
    if (tel) {
      const tp = eventPayload(tel);
      const called = Array.isArray(tp.tools_called) ? tp.tools_called.length : null;
      const succeededCount = Array.isArray(tp.tools_succeeded)
        ? tp.tools_succeeded.length
        : typeof tp.tools_succeeded === "number"
          ? tp.tools_succeeded
          : null;
      if (succeededCount != null) {
        chips.push({
          label: called ? `${succeededCount}/${called} tools` : `${succeededCount} tools`,
          type: "neutral",
        });
      }
    }
  }

  if (agentId === "muhtasib") {
    const verdict = [...ordered].reverse().find((e) => e.event_type === "verdict_issued");
    if (verdict) {
      const vp = eventPayload(verdict);
      const decision = vp.decision || vp.verdict || "";
      if (/CHALLENGE/i.test(decision)) chips.push({ label: "Challenge", type: "warning" });
      else if (/APPROVED|PASSED/i.test(decision)) chips.push({ label: "Safety approved", type: "good" });
      if (vp.risk_score != null) chips.push({ label: `Risk: ${vp.risk_score}`, type: "neutral" });
      if (vp.challenge_target === "PLAN") chips.push({ label: "Plan revision", type: "warning" });
      else if (vp.challenge_target === "EVIDENCE") chips.push({ label: "Evidence challenge", type: "warning" });
    }
  }

  if (agentId === "mudabbir") {
    const plan = [...ordered].reverse().find((e) => e.event_type === "plan_created");
    if (plan) {
      const pp = eventPayload(plan);
      const planObj = pp.plan && typeof pp.plan === "object" ? pp.plan : pp;
      const actionCount = pp.action_count ?? planObj.actions?.length;
      if (actionCount != null) chips.push({ label: `${actionCount} action${actionCount !== 1 ? "s" : ""}`, type: "neutral" });
      const risk = planObj.risk_level || pp.risk_level;
      if (risk) chips.push({ label: `Risk: ${risk}`, type: risk === "high" || risk === "critical" ? "warning" : "neutral" });
    }
  }

  if (agentId === "aamil") {
    const exec = ordered.find((e) => e.event_type === "actions_executed");
    if (exec) {
      const ep = eventPayload(exec);
      const receipts = ep.reconciliation?.receipts || ep.receipts || {};
      const receiptList = Object.values(receipts);
      const realReceipt = receiptList.find((r) => r?.is_real_mutation === true);
    if (realReceipt) chips.push({ label: "Sandbox mutation", type: "good" });
      if (realReceipt?.detail?.http_status === 200) chips.push({ label: "HTTP 200", type: "good" });
    }
    const recovery = ordered.find((e) => e.event_type === "recovery_verified");
    if (recovery) {
      const rp = eventPayload(recovery);
      const status = rp.status || rp.recovery_status;
      if (status === "RECOVERED" || status === "recovered") chips.push({ label: "Health verified", type: "good" });
    }
  }

  return chips;
}

/* ── Challenge Spotlight (Item 7) ────────────────────────────────────────── */

function ChallengeSpotlight({ events }) {
  const ordered = [...(events || [])].sort(compareEventSequence);
  const spotlights = [];
  const seenKeys = new Set();

  ordered.forEach((evt) => {
    if (evt.event_type !== "verdict_issued") return;
    const vp = eventPayload(evt);
    const decision = vp.decision || vp.verdict || "";
    if (/CHALLENGE/i.test(decision)) {
      const key = `ch-${evt.event_hash || evt.event_id || evt.sequence}`;
      if (!seenKeys.has(key)) {
        seenKeys.add(key);
        spotlights.push({
          type: "challenge",
          text: vp.reasoning || vp.challenge_reason || "Safety challenge triggered — plan revision required",
          key,
        });
      }
    }
  });

  // Check if a revision was accepted (latest verdict is APPROVED after a CHALLENGE)
  const verdicts = ordered.filter((e) => e.event_type === "verdict_issued");
  if (verdicts.length > 1) {
    const latest = verdicts[verdicts.length - 1];
    const lp = eventPayload(latest);
    const latestDecision = lp.decision || lp.verdict || "";
    if (/APPROVED|PASSED/i.test(latestDecision)) {
      const key = `acc-${latest.event_hash || latest.event_id || latest.sequence}`;
      if (!seenKeys.has(key)) {
        seenKeys.add(key);
        spotlights.push({
          type: "accepted",
          text: lp.reasoning || "Revised plan accepted after safety review",
          key,
        });
      }
    }
  }

  if (spotlights.length === 0) return null;

  // Show only the latest spotlight to avoid stacking banners that crush messages
  const challengeCount = spotlights.filter((s) => s.type === "challenge").length;
  const lastSpotlight = spotlights[spotlights.length - 1];

  return (
    <div key={lastSpotlight.key} className={`challenge-spotlight challenge-spotlight--${lastSpotlight.type}`}>
      <span className="challenge-spotlight__icon">
        {lastSpotlight.type === "challenge" ? "ALERT" : "PASS"}
      </span>
      <div className="challenge-spotlight__content">
        <span className="challenge-spotlight__label">
          {lastSpotlight.type === "challenge"
            ? `SAFETY CHALLENGE${challengeCount > 1 ? ` (${challengeCount} of ${challengeCount})` : ""}`
            : `REVISION ACCEPTED${challengeCount > 0 ? ` after ${challengeCount} challenge${challengeCount > 1 ? "s" : ""}` : ""}`}
        </span>
        <span className="challenge-spotlight__text">{lastSpotlight.text}</span>
      </div>
    </div>
  );
}

/* ── Activity Strip (Item 9 — status-driven) ─────────────────────────────── */

function ActivityStrip({ incidentStatus, events, streamComplete }) {
  // Hard kill: never show on terminal or awaiting approval
  if (TERMINAL_STATUSES.includes(incidentStatus) || incidentStatus === "AWAITING_APPROVAL") {
    return null;
  }
  if (streamComplete) return null;

  const activity = ACTIVITY_MESSAGES[incidentStatus];
  if (!activity) return null;

  // Check if the activity's associated event has already been emitted
  const ordered = [...(events || [])].sort(compareEventSequence);
  const eventMap = {
    DETECTED: "investigation_completed",
    ANALYZING: "investigation_completed",
    PLANNING: "plan_created",
    REVIEWING: "verdict_issued",
    EXECUTING: "actions_executed",
  };

  const completionEvent = eventMap[incidentStatus];
  if (completionEvent) {
    // For PLANNING/REVIEWING, we need to check if the LATEST cycle is complete
    const relevantEvents = ordered.filter((e) => e.event_type === completionEvent);
    // If there are challenge cycles, the latest verdict for current plan matters
    // Simple heuristic: if the last event of this type is after the last status transition, hide
    if (incidentStatus === "REVIEWING") {
      const lastPlan = [...ordered].reverse().find((e) => e.event_type === "plan_created");
      const lastVerdict = [...ordered].reverse().find((e) => e.event_type === "verdict_issued");
      if (lastVerdict && lastPlan && (lastVerdict.sequence ?? 0) > (lastPlan.sequence ?? 0)) {
        return null; // Verdict already issued for current plan
      }
    } else if (relevantEvents.length > 0) {
      // Generic: if completion event exists and we're still in this status, it might be a cycle
      // Only hide if status should have transitioned
    }
  }

  const persona = AGENT_PERSONAS[activity.agent];

  return (
    <div className="activity-strip" style={{ "--agent-accent": persona?.accent || "var(--cyan)" }}>
      <span className="activity-strip__dot" />
      <span className="activity-strip__text">{activity.text}</span>
    </div>
  );
}

/* ── Evidence Chip Rendering ─────────────────────────────────────────────── */

function EvidenceChips({ chips }) {
  if (!chips || chips.length === 0) return null;
  return (
    <div className="evidence-chips">
      {chips.map((chip, i) => (
        <span key={i} className={`evidence-chip evidence-chip--${chip.type}`}>
          {chip.label}
        </span>
      ))}
    </div>
  );
}

/* ── Main Component ──────────────────────────────────────────────────────── */

export default function AgentRoom({
  roomMessages,
  events,
  incidentStatus,
  streamComplete,
}) {
  const bottomRef = useRef(null);
  const containerRef = useRef(null);

  // Auto-scroll only when user is near the bottom
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !bottomRef.current) return;
    const isNearBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight < 120;
    if (isNearBottom) {
      bottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [roomMessages.length]);

  // ── Filter: only curated agent messages ──
  const visibleMessages = filterAgentRoomMessages(roomMessages);

  // ── Precompute per-agent evidence chips from structured events ──
  const chipsByAgent = useMemo(() => {
    const result = {};
    const agentIds = ["nigehban", "muhaqqiq", "mudabbir", "muhtasib", "aamil"];
    for (const id of agentIds) {
      result[id] = deriveEvidenceChips(id, events);
    }
    return result;
  }, [events]);

  // Track which agents have already shown their chips
  const chipsShownForAgent = useRef(new Set());
  useEffect(() => {
    chipsShownForAgent.current = new Set();
  }, [events]);

  // ── Find LATEST contract_issued event for authority boundary ──
  const orderedEvents = [...(events || [])].sort(compareEventSequence);
  const contractEvent = [...orderedEvents]
    .reverse()
    .find((e) => e.event_type === "contract_issued");
  const contractPayload = contractEvent ? eventPayload(contractEvent) : {};
  const contractTimestamp = contractEvent
    ? new Date(contractEvent.timestamp || contractEvent.created_at || 0).getTime()
    : null;

  // ── Correlate contract to matching plan for action count + expiry ──
  let matchedPlanActions = null;
  let contractExpiry = contractPayload.expires_at || contractPayload.token_expires_at || null;
  if (contractEvent) {
    const contractIdx = orderedEvents.indexOf(contractEvent);
    const candidatePlans = orderedEvents.slice(0, contractIdx).reverse();
    const matchedPlan = candidatePlans.find((e) => {
      if (e.event_type !== "plan_created") return false;
      const ep = eventPayload(e);
      const plan = ep.plan && typeof ep.plan === "object" ? ep.plan : ep;
      // Match by revision, plan_id, plan_hash
      if (contractPayload.revision != null && (plan.revision ?? ep.revision) !== contractPayload.revision) return false;
      if (contractPayload.plan_id && (plan.plan_id ?? ep.plan_id) && (plan.plan_id ?? ep.plan_id) !== contractPayload.plan_id) return false;
      if (contractPayload.plan_hash && (ep.plan_hash ?? plan.plan_hash) && (ep.plan_hash ?? plan.plan_hash) !== contractPayload.plan_hash) return false;
      return true;
    });
    if (matchedPlan) {
      const mp = eventPayload(matchedPlan);
      const plan = mp.plan && typeof mp.plan === "object" ? mp.plan : mp;
      matchedPlanActions = Array.isArray(plan.actions) ? plan.actions.length : (mp.action_count ?? plan.action_count ?? null);
    }
  }

  // ── Insert boundary: find the message index where contract comes after ──
  let boundaryAfterIndex = -1;
  if (contractTimestamp) {
    for (let i = visibleMessages.length - 1; i >= 0; i--) {
      const msgTime = new Date(
        visibleMessages[i].timestamp || visibleMessages[i].created_at || 0
      ).getTime();
      if (msgTime <= contractTimestamp) {
        boundaryAfterIndex = i;
        break;
      }
    }
    // If all messages are before contract, put boundary at end
    if (boundaryAfterIndex === -1 && visibleMessages.length > 0) {
      boundaryAfterIndex = visibleMessages.length - 1;
    }
  }

  if (visibleMessages.length === 0) {
    const failedStatuses = ["PIPELINE_FAILED", "EXECUTION_FAILED", "RECOVERY_FAILED"];
    const isFailed = failedStatuses.includes(incidentStatus);
    const isTerminal = TERMINAL_STATUSES.includes(incidentStatus);
    return (
      <div className="room-panel">
        <CouncilPresenceBar
          incidentStatus={incidentStatus}
          events={events}
        />
        <div className="empty-state">
          {isFailed ? (
            <>
              <span className="empty-state__icon" style={{ filter: "grayscale(1)" }}>⚠️</span>
              <span className="empty-state__text">
                Pipeline failed during {incidentStatus.replace(/_/g, " ").toLowerCase()}
              </span>
              <span className="empty-state__hint">
                Open Evidence to inspect the event chain
              </span>
            </>
          ) : isTerminal ? (
            <>
              <span className="empty-state__icon">✅</span>
              <span className="empty-state__text">
                Incident resolved
              </span>
              <span className="empty-state__hint">
                View the Resolution Record in Approvals
              </span>
            </>
          ) : (
            <>
              <span className="empty-state__icon">LIVE</span>
              <span className="empty-state__text">
                Waiting for agent activity…
              </span>
              <span className="empty-state__hint">
                Agents will appear here as they work on the incident
              </span>
            </>
          )}
        </div>
        <ActivityStrip
          incidentStatus={incidentStatus}
          events={events}
          streamComplete={streamComplete}
        />
      </div>
    );
  }

  return (
    <div className="room-panel">
      <CouncilPresenceBar
        incidentStatus={incidentStatus}
        events={events}
      />

      {/* Challenge Spotlight (Item 7) */}
      <ChallengeSpotlight events={events} />

      <div className="room-messages" ref={containerRef} aria-live="polite">
        {visibleMessages.map((msg, i) => {
          const senderId = String(
            msg.sender || msg.actor || ""
          ).toLowerCase();
          const persona = AGENT_PERSONAS[senderId];
          if (!persona) return null;

          const msgType = resolveMessageType(msg);
          const msgKey =
            msg.message_id || msg.event_id || `msg-${i}-${msg.timestamp}`;
          const role = AGENT_ROLES[senderId] || "";

          // Show evidence chips on the LAST message from each agent
          const isLastFromAgent = !visibleMessages.slice(i + 1).some(
            (m) => String(m.sender || m.actor || "").toLowerCase() === senderId
          );
          const agentChips = isLastFromAgent ? chipsByAgent[senderId] : [];

          return (
            <div key={msgKey}>
              <div
                className={`council-message council-message--${msgType}`}
                style={{ "--agent-accent": persona.accent }}
              >
                <div className="council-message__avatar-col">
                  <img
                    className="council-message__avatar"
                    src={persona.avatar}
                    alt={`AI Agent: ${persona.displayName}`}
                    onError={(e) => {
                      e.currentTarget.onerror = null;
                      e.currentTarget.src = persona.fallbackAvatar;
                    }}
                  />
                </div>
                <div className="council-message__body">
                  {/* Stronger header bar (Item 6) */}
                  <div className="council-message__header">
                    <span className="council-message__agent">
                      <span
                        className="council-message__name"
                        style={{ color: persona.accent }}
                      >
                        {persona.displayName.toUpperCase()}
                      </span>
                      <span className="council-message__role">
                        {role}
                        {msgType === "challenge" ? " · CHALLENGE" : ""}
                      </span>
                      <span className="council-message__ai-label">
                        AI Agent
                      </span>
                    </span>
                    <span className="council-message__meta">
                      <span
                        className={`msg-type-badge msg-type-badge--${msgType}`}
                      >
                        {MESSAGE_TYPE_LABELS[msgType] || msgType}
                      </span>
                      <span className="council-message__time">
                        {formatTime(msg.timestamp)}
                      </span>
                    </span>
                  </div>
                  <div className="council-message__content">
                    {renderMentions(msg.content || msg.summary || "")}
                  </div>
                  {/* Structured evidence chips (Item 8) — from event payloads only */}
                  <EvidenceChips chips={agentChips} />
                </div>
              </div>

              {i === boundaryAfterIndex && (() => {
                const cId = contractPayload.contract_id || "";
                const rev = contractPayload.revision ?? "";
                const hash = (contractPayload.plan_hash || "").slice(0, 8);
                const actionCount = matchedPlanActions
                  ?? (Array.isArray(contractPayload.actions) ? contractPayload.actions.length : contractPayload.action_count ?? "");
                return (
                  <div className="authority-boundary">
                    <span className="authority-boundary__line" />
                    <span className="authority-boundary__label">
                      OPERATOR AUTHORITY BOUNDARY
                    </span>
                    {cId && (
                      <span className="authority-boundary__meta">
                        {cId}{rev ? ` · Revision ${rev}` : ""}
                        {actionCount ? ` · ${actionCount} action${actionCount !== 1 ? "s" : ""}` : ""}
                        {hash ? ` · Plan hash ${hash}…` : ""}
                        {contractExpiry ? ` · Expires ${new Date(contractExpiry).toLocaleTimeString()}` : ""}
                      </span>
                    )}
                    <span className="authority-boundary__disclaimer">
                      Nothing executes until this exact contract is authorized.
                    </span>
                    <span className="authority-boundary__line" />
                  </div>
                );
              })()}
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>

      {/* Activity Strip (Item 9 — status-driven, not message-sender-driven) */}
      <ActivityStrip
        incidentStatus={incidentStatus}
        events={events}
        streamComplete={streamComplete}
      />
    </div>
  );
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
