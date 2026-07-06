/* ═══════════════════════════════════════════════════════════════════════════════
 * MuhafizSRE — Event Selectors Library
 * ═══════════════════════════════════════════════════════════════════════════════
 * Canonical accessors for structured event data. Handles REST vs SSE payload
 * differences, nested plan structures, revision-correlated history, and
 * ordered lifecycle state derivation.
 * ═══════════════════════════════════════════════════════════════════════════════ */

import { AGENT_PERSONAS, TERMINAL_STATUSES } from "../personas";

/* ── 1. Payload normalizer ──────────────────────────────────────────────── */

/** Normalize event payload — REST and SSE may expose payloads differently */
export function eventPayload(event) {
  if (event.payload && typeof event.payload === "object") return event.payload;
  try {
    return JSON.parse(event.payload_json || "{}");
  } catch {
    return {};
  }
}

/* ── 2. Plan event canonical accessor ───────────────────────────────────── */

/**
 * Canonical accessor for plan_created event data.
 * Handles the nested payload.plan structure:
 *
 *   {
 *     "action_count": 1,
 *     "plan": {
 *       "plan_id": "PLAN-4AB43F38",
 *       "revision": 2,
 *       "actions": [...],
 *       "risk_level": "medium",
 *       "strategy_summary": "..."
 *     },
 *     "plan_hash": "c2151983..."
 *   }
 */
export function planEventData(event) {
  const payload = eventPayload(event);
  const plan =
    payload.plan && typeof payload.plan === "object" ? payload.plan : payload;

  return {
    payload,
    plan,
    revision: plan.revision ?? payload.revision ?? null,
    planId: plan.plan_id ?? payload.plan_id ?? null,
    planHash: payload.plan_hash ?? plan.plan_hash ?? null,
    actions: Array.isArray(plan.actions) ? plan.actions : [],
    riskLevel: plan.risk_level ?? payload.risk_level ?? null,
    strategySummary: plan.strategy_summary ?? payload.strategy_summary ?? "",
    actionCount: payload.action_count ?? plan.actions?.length ?? 0,
  };
}

/* ── 3. Verdict bounded to plan window ──────────────────────────────────── */

/**
 * Find the verdict_issued event that belongs to a specific plan,
 * bounded by the next plan_created or contract_issued event.
 *
 * The real verdict_issued payload has NO revision field. It contains
 * decision, reasoning, risk_score, challenge fields and retry metadata.
 * We must bound by sequence position, not by matching a revision number.
 */
export function findVerdictForPlan(events, planEvt) {
  if (!planEvt) return null;

  const planIndex = events.indexOf(planEvt);
  if (planIndex === -1) return null;

  const nextBoundaryOffset = events
    .slice(planIndex + 1)
    .findIndex(
      (e) =>
        e.event_type === "plan_created" ||
        e.event_type === "contract_issued"
    );

  const windowEnd =
    nextBoundaryOffset === -1
      ? events.length
      : planIndex + 1 + nextBoundaryOffset;

  return (
    events
      .slice(planIndex + 1, windowEnd)
      .find((e) => e.event_type === "verdict_issued") ?? null
  );
}

/* ── 4. Council Recommendation ──────────────────────────────────────────── */

/**
 * Derive Council Recommendation from structured events only.
 * When contractRevision is provided (Approval/Resolution views),
 * match plan to that revision via planEventData(), then bound
 * verdict to the plan window.
 * Otherwise use latest (active workflow view).
 */
export function deriveCouncilRecommendation(events, contractRevision = null) {
  const sorted = [...events].sort(
    (a, b) => (a.sequence ?? 0) - (b.sequence ?? 0)
  );

  const triageEvt = sorted.find((e) => e.event_type === "triage_completed");

  let planEvt, verdictEvt;

  if (contractRevision != null) {
    // Match plan to specific contract revision using planEventData
    planEvt = [...sorted].reverse().find((e) => {
      if (e.event_type !== "plan_created") return false;
      return planEventData(e).revision === contractRevision;
    });
    // Bound verdict to the matched plan's window
    verdictEvt = findVerdictForPlan(sorted, planEvt);
  } else {
    // Fallback: latest plan
    planEvt = [...sorted].reverse().find((e) => e.event_type === "plan_created");
    verdictEvt = findVerdictForPlan(sorted, planEvt);
  }

  // Select the latest investigation_completed BEFORE the matched plan.
  // After a challenge/revision cycle, the first investigation may no longer
  // support the approved plan — use the one that preceded it.
  const planIndex = planEvt ? sorted.indexOf(planEvt) : sorted.length;
  const investigationEvt = [...sorted.slice(0, planIndex)]
    .reverse()
    .find((e) => e.event_type === "investigation_completed")
    // Fallback: if no investigation before plan, use earliest (first-pass)
    || sorted.find((e) => e.event_type === "investigation_completed")
    || null;

  return {
    triage: triageEvt
      ? { event: triageEvt, payload: eventPayload(triageEvt) }
      : null,
    investigation: investigationEvt
      ? { event: investigationEvt, payload: eventPayload(investigationEvt) }
      : null,
    plan: planEvt ? { event: planEvt, data: planEventData(planEvt) } : null,
    verdict: verdictEvt
      ? { event: verdictEvt, payload: eventPayload(verdictEvt) }
      : null,
  };
}

/* ── 5. Approval history reconstruction ─────────────────────────────────── */

/**
 * Reconstruct approval history for terminal incidents.
 * Correlate by contract_id → revision → plan_hash → sequence.
 *
 * - Candidate plans restricted to events BEFORE the contract
 * - Post-contract events matched by contract_id, then revision, then sequence
 */
export function reconstructApprovalHistory(events) {
  const sorted = [...events].sort(
    (a, b) => (a.sequence ?? 0) - (b.sequence ?? 0)
  );

  const contractEvt = [...sorted]
    .reverse()
    .find((e) => e.event_type === "contract_issued");
  if (!contractEvt) return null;

  const cp = eventPayload(contractEvt);
  const contractId = cp.contract_id;
  const revision = cp.revision;
  const planHash = cp.plan_hash;
  const planIdFromContract = cp.plan_id;
  const contractIdx = sorted.indexOf(contractEvt);

  // ── Match plan_created: MUST be before the contract ──
  const planEvt = [...sorted.slice(0, contractIdx)]
    .reverse()
    .find((e) => {
      if (e.event_type !== "plan_created") return false;
      const data = planEventData(e);
      if (cp.revision != null && data.revision !== cp.revision) return false;
      if (planIdFromContract && data.planId && data.planId !== planIdFromContract)
        return false;
      if (planHash && data.planHash && data.planHash !== planHash) return false;
      return true;
    });

  // ── Verdict bounded to plan window ──
  const verdictEvt = findVerdictForPlan(sorted, planEvt);

  // ── Post-contract events: correlate by contract_id → revision → sequence ──
  const postContract = sorted.slice(contractIdx);

  function findCorrelated(eventType) {
    // Priority 1: match by contract_id
    const byContractId = postContract.find((e) => {
      if (e.event_type !== eventType) return false;
      const p = eventPayload(e);
      return p.contract_id === contractId;
    });
    if (byContractId) return byContractId;

    // Priority 2: match by revision
    const byRevision = postContract.find((e) => {
      if (e.event_type !== eventType) return false;
      const p = eventPayload(e);
      return p.revision === revision;
    });
    if (byRevision) return byRevision;

    // Priority 3: first occurrence after contract in sequence
    return postContract.find((e) => e.event_type === eventType) || null;
  }

  const approvalEvt = findCorrelated("human_approved");
  const rejectionEvt = findCorrelated("human_rejected");
  const executionEvt = findCorrelated("actions_executed");
  const recoveryEvt = findCorrelated("recovery_verified");
  // outcome and seal are incident-level (one per incident)
  const outcomeEvt = sorted.find((e) => e.event_type === "outcome");
  const sealEvt = sorted.find((e) => e.event_type === "seal");

  return {
    contractEvt,
    contractPayload: cp,
    planEvt,
    planData: planEvt ? planEventData(planEvt) : null,
    verdictEvt,
    verdictPayload: verdictEvt ? eventPayload(verdictEvt) : null,
    approvalEvt,
    rejectionEvt,
    executionEvt,
    recoveryEvt,
    outcomeEvt,
    sealEvt,
    contractId,
    revision,
    planHash,
    planId: planIdFromContract,
  };
}

/* ── 6. Agent lifecycle state ───────────────────────────────────────────── */

/**
 * Derive agent lifecycle state from ORDERED events.
 * Uses latest relevant event for verdict state — a Set would lose order.
 */
export function deriveAgentState(agentId, events, incidentStatus) {
  const persona = AGENT_PERSONAS[agentId];
  if (!persona) return "not_involved";

  if (TERMINAL_STATUSES.includes(incidentStatus)) {
    const participated = events.some((e) =>
      persona.completeEvents.includes(e.event_type)
    );
    return participated ? "completed" : "not_involved";
  }

  // ── Current-stage ownership (beats historical completion or challenge) ──
  // Muhtasib active when reviewing, even after a prior CHALLENGE
  if (agentId === "muhtasib" && incidentStatus === "REVIEWING") {
    return "active";
  }
  // Mudabbir may have an earlier plan_created but is actively revising
  if (agentId === "mudabbir" && incidentStatus === "PLANNING") {
    return "active";
  }
  // Muhaqqiq may have an earlier investigation_completed but is re-investigating
  if (
    agentId === "muhaqqiq" &&
    incidentStatus === "ANALYZING" &&
    events.some((e) => e.event_type === "triage_completed")
  ) {
    return "active";
  }
  if (agentId === "aamil" && incidentStatus === "EXECUTING") return "active";
  if (agentId === "aamil" && incidentStatus === "AWAITING_APPROVAL")
    return "waiting";

  // ── Latest verdict (only checked after current-stage ownership) ──
  if (agentId === "muhtasib") {
    const latestVerdict = [...events]
      .reverse()
      .find((e) => e.event_type === "verdict_issued");
    if (latestVerdict) {
      const vp = eventPayload(latestVerdict);
      const decision =
        vp.verdict || vp.decision || latestVerdict.summary || "";
      if (decision.includes("CHALLENGE")) return "challenged";
      if (decision.includes("APPROVED") || decision.includes("PASSED"))
        return "completed";
    }
  }

  // ── Historical completion ──
  const isComplete = events.some((e) =>
    persona.completeEvents.includes(e.event_type)
  );
  if (isComplete) return "completed";

  if (persona.activeDuring.includes(incidentStatus)) return "active";

  return "waiting";
}

/* ── 7. Telemetry extraction ────────────────────────────────────────────── */

/** Telemetry lives in events, never roomMessages */
export function extractTelemetry(events) {
  return (events || []).filter(
    (e) => e.event_type === "agent_usage_telemetry"
  );
}

/* ── 8. Room message sorting & filtering ────────────────────────────────── */

/** Compare room messages: sequence first, timestamp fallback */
export function compareRoomMessages(a, b) {
  if (a.sequence != null && b.sequence != null)
    return a.sequence - b.sequence;
  const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
  const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
  return ta - tb;
}

/**
 * Filter room messages to only curated agent messages.
 * Excludes gateway, system, telemetry, and non-agent senders.
 */
export function filterAgentRoomMessages(roomMessages) {
  const seen = new Set();
  return roomMessages
    .filter((message) => {
      const sender = String(message.sender || message.actor || "").toLowerCase();
      const type = message.message_type || message.msg_type || "";

      return (
        Boolean(AGENT_PERSONAS[sender]) &&
        type !== "system" &&
        type !== "agent_usage_telemetry"
      );
    })
    .sort(compareRoomMessages)
    .filter((m) => {
      const key =
        m.message_id || m.event_id || `${m.sender}-${m.timestamp}-${m.sequence}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}
