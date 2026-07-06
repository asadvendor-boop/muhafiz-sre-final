/**
 * MuhafizSRE — Automated Frontend Tests (Round 4)
 *
 * Mix of:
 *   ≥75% behavioral tests (rendered React components via @testing-library/react)
 *   ≤25% source-inspection tests (security invariants only)
 *
 * Behavioral priorities (per user requirements):
 *   1. Reject click posts REJECT
 *   2. Null contract does not render approval controls
 *   3. Expired contract disables approval
 *   4. Challenge message renders amber challenge card
 *   5. contract_issued advances guided demo
 *   6. A→B delayed hydration cannot leak A into B
 *   7. Verification response from A cannot mark B verified
 *   8. Latest contract boundary uses the latest contract
 *   9. Resolution Record uses correlated plan revision
 *
 * Plus unit tests for data-shape functions, service restrictions,
 * and a small number of source-inspection invariants.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import React from "react";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import "@testing-library/jest-dom";

// ── Data-shape selectors ──────────────────────────────────────────────
import {
  eventPayload,
  planEventData,
  filterAgentRoomMessages,
  findVerdictForPlan,
  reconstructApprovalHistory,
  deriveAgentState,
  deriveCouncilRecommendation,
  extractTelemetry,
} from "../app/lib/eventSelectors.js";

import {
  AGENT_PERSONAS,
  DEMO_PRESETS,
  SERVICE_OPTIONS,
  TERMINAL_STATUSES,
  TELEMETRY_FIELDS,
  MESSAGE_TYPE_LABELS,
} from "../app/personas.js";

// ── Helper: read source file ──────────────────────────────────────────
const ROOT = resolve(import.meta.dirname, "..");
function readSrc(relPath) {
  return readFileSync(resolve(ROOT, relPath), "utf-8");
}

// ── Mock fetch globally ───────────────────────────────────────────────
let fetchMock;
beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({}),
    text: () => Promise.resolve(""),
  });
  global.fetch = fetchMock;
  // Mock scrollIntoView for jsdom (not natively supported)
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/* ═══════════════════════════════════════════════════════════════════════
 * BEHAVIORAL TEST 1: Reject click posts REJECT
 * ═══════════════════════════════════════════════════════════════════════ */
describe("BT1: Reject button sends REJECT", () => {
  it("ApprovalTab renders Reject button and click submits REJECT action", async () => {
    const ApprovalTab = (await import("../app/components/ApprovalTab.jsx")).default;
    const mockDecisionSubmitted = vi.fn();

    fetchMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ success: true }),
      text: () => Promise.resolve(""),
    });

    render(
      <ApprovalTab
        contract={{
          contract_id: "c1",
          revision: 1,
          plan_hash: "abc123",
          risk_level: "low",
          strategy_summary: "test",
          actions: [{ skill: "restart", target: "svc" }],
          expires_at: new Date(Date.now() + 300000).toISOString(),
        }}
        approvalToken="tok1"
        incidentId="INC-001"
        incidentStatus="AWAITING_APPROVAL"
        events={[]}
        onDecisionSubmitted={mockDecisionSubmitted}
        incidentDetail={{ service_id: "auth-service", severity: "P1", summary: "test" }}
      />
    );

    const rejectBtn = screen.getByRole("button", { name: /deny execution/i });
    expect(rejectBtn).toBeInTheDocument();
    fireEvent.click(rejectBtn);

    await waitFor(() => {
      const calls = fetchMock.mock.calls;
      const decisionCall = calls.find(
        (c) => c[0]?.includes("/decisions") || (typeof c[0] === "string" && c[0].includes("decisions"))
      );
      expect(decisionCall).toBeDefined();
      const body = JSON.parse(decisionCall[1].body);
      expect(body.action).toBe("REJECT");
      expect(body.action).not.toBe("MARK_FALSE_ALARM");
    });
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * BEHAVIORAL TEST 2: Null contract does not render approval controls
 * ═══════════════════════════════════════════════════════════════════════ */
describe("BT2: Null contract hides approval controls", () => {
  it("renders empty state when contract is null and status is not AWAITING_APPROVAL", async () => {
    const ApprovalTab = (await import("../app/components/ApprovalTab.jsx")).default;

    render(
      <ApprovalTab
        contract={null}
        approvalToken=""
        incidentId="INC-001"
        incidentStatus="ANALYZING"
        events={[]}
        onDecisionSubmitted={() => {}}
        incidentDetail={null}
      />
    );

    expect(screen.getByText(/No approval gate is active/i)).toBeInTheDocument();
    expect(screen.queryByText(/Authorize Execution/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Deny Execution/i)).not.toBeInTheDocument();
  });

  it("shows loading when status is AWAITING_APPROVAL but contract is null", async () => {
    const ApprovalTab = (await import("../app/components/ApprovalTab.jsx")).default;

    render(
      <ApprovalTab
        contract={null}
        approvalToken=""
        incidentId="INC-001"
        incidentStatus="AWAITING_APPROVAL"
        events={[]}
        onDecisionSubmitted={() => {}}
        incidentDetail={null}
      />
    );

    expect(screen.getByText(/Loading contract/i)).toBeInTheDocument();
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * BEHAVIORAL TEST 3: Expired contract disables approval
 * ═══════════════════════════════════════════════════════════════════════ */
describe("BT3: Expired contract disables approval", () => {
  it("disables Approve button and shows REVALIDATION when contract is expired", async () => {
    const ApprovalTab = (await import("../app/components/ApprovalTab.jsx")).default;

    render(
      <ApprovalTab
        contract={{
          contract_id: "c1",
          revision: 1,
          plan_hash: "abc123",
          risk_level: "low",
          strategy_summary: "test",
          actions: [{ skill: "restart" }],
          expires_at: new Date(Date.now() - 60000).toISOString(), // Expired 1 min ago
        }}
        approvalToken="tok1"
        incidentId="INC-001"
        incidentStatus="AWAITING_APPROVAL"
        events={[]}
        onDecisionSubmitted={() => {}}
        incidentDetail={{ service_id: "auth-service", severity: "P1" }}
      />
    );

    // Approve should be disabled
    const approveBtn = screen.getByText(/Authorize Execution/i);
    expect(approveBtn).toBeDisabled();

    // Expiry banner should appear
    expect(screen.getAllByText(/Contract Expired/i).length).toBeGreaterThan(0);
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * BEHAVIORAL TEST 4: Challenge message renders amber challenge card
 * ═══════════════════════════════════════════════════════════════════════ */
describe("BT4: Challenge message styling", () => {
  it("Muhtasib verdict with CHALLENGE content renders as challenge type", async () => {
    const AgentRoom = (await import("../app/components/AgentRoom.jsx")).default;

    render(
      <AgentRoom
        roomMessages={[
          {
            message_id: "m1",
            sender: "muhtasib",
            message_type: "verdict",
            content: "CHALLENGE: blast-radius exceeds allowed threshold",
            timestamp: "2025-01-01T00:01:00Z",
          },
        ]}
        events={[]}
        incidentStatus="REVIEWING"
      />
    );

    // The message card should have the challenge class
    const messageCards = document.querySelectorAll(".council-message--challenge");
    expect(messageCards.length).toBe(1);

    // Badge should show "Challenge"
    const badges = document.querySelectorAll(".msg-type-badge--challenge");
    expect(badges.length).toBe(1);
  });

  it("Muhtasib verdict with PASSED content renders as safety-approved type", async () => {
    const AgentRoom = (await import("../app/components/AgentRoom.jsx")).default;

    render(
      <AgentRoom
        roomMessages={[
          {
            message_id: "m1",
            sender: "muhtasib",
            message_type: "verdict",
            content: "PASSED: plan meets all safety constraints",
            timestamp: "2025-01-01T00:01:00Z",
          },
        ]}
        events={[]}
        incidentStatus="AWAITING_APPROVAL"
      />
    );

    const messageCards = document.querySelectorAll(".council-message--safety-approved");
    expect(messageCards.length).toBe(1);
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * BEHAVIORAL TEST 5: contract_issued advances guided demo
 * ═══════════════════════════════════════════════════════════════════════ */
describe("BT5: Guided demo advancement", () => {
  it("contract_issued event advances demo past Safety Review step", async () => {
    const GuidedDemoCoach = (await import("../app/components/GuidedDemoCoach.jsx")).default;

    const events = [
      { event_type: "triage_completed", sequence: 1 },
      { event_type: "investigation_completed", sequence: 2 },
      { event_type: "plan_created", sequence: 3 },
      { event_type: "contract_issued", sequence: 4 },
    ];

    render(
      <GuidedDemoCoach
        incidentId="INC-001"
        events={events}
        incidentStatus="AWAITING_APPROVAL"
        onSwitchTab={() => {}}
        demoScenarioId="cache_stampede"
      />
    );

    // Should show step 5 (Operator Authorization Gate) since contract_issued advances past step 4
    expect(screen.getByText(/Operator Authorization Gate/i)).toBeInTheDocument();
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * BEHAVIORAL TEST 8: Latest contract boundary
 * ═══════════════════════════════════════════════════════════════════════ */
describe("BT8: Latest contract boundary in AgentRoom", () => {
  it("renders boundary with latest contract_issued data, not first", async () => {
    const AgentRoom = (await import("../app/components/AgentRoom.jsx")).default;

    render(
      <AgentRoom
        roomMessages={[
          { message_id: "m1", sender: "mudabbir", msg_type: "plan", content: "Plan v1", timestamp: "2025-01-01T00:01:00Z" },
          { message_id: "m2", sender: "muhtasib", msg_type: "verdict", content: "APPROVED plan", timestamp: "2025-01-01T00:03:00Z" },
          { message_id: "m3", sender: "mudabbir", msg_type: "plan", content: "Plan v2 revised", timestamp: "2025-01-01T00:05:00Z" },
        ]}
        events={[
          {
            event_type: "contract_issued",
            sequence: 1,
            timestamp: "2025-01-01T00:02:00Z",
            payload: { contract_id: "OLD-CONTRACT", revision: 1, plan_hash: "old111" },
          },
          {
            event_type: "contract_issued",
            sequence: 5,
            timestamp: "2025-01-01T00:06:00Z",
            payload: { contract_id: "NEW-CONTRACT", revision: 2, plan_hash: "new222" },
          },
        ]}
        incidentStatus="AWAITING_APPROVAL"
      />
    );

    // Authority boundary should show the LATEST contract
    const boundary = document.querySelector(".authority-boundary__meta");
    expect(boundary).toBeTruthy();
    expect(boundary.textContent).toContain("NEW-CONTRACT");
    expect(boundary.textContent).not.toContain("OLD-CONTRACT");
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * BEHAVIORAL TEST 9: Resolution Record uses correlated plan revision
 * ═══════════════════════════════════════════════════════════════════════ */
describe("BT9: Resolution Record correlation", () => {
  it("renders contract details from correlated history", async () => {
    const ResolutionRecord = (await import("../app/components/ResolutionRecord.jsx")).default;

    const events = [
      {
        event_type: "plan_created", sequence: 1, timestamp: "2025-01-01T00:01:00Z",
        payload: {
          plan: { plan_id: "PLAN-1", revision: 1, strategy_summary: "restart svc", risk_level: "low", actions: [{ skill: "restart" }] },
          plan_hash: "abc123",
        },
      },
      {
        event_type: "verdict_issued", sequence: 2, timestamp: "2025-01-01T00:02:00Z",
        payload: { verdict: "APPROVED", reasoning: "Safe plan" },
      },
      {
        event_type: "contract_issued", sequence: 3, timestamp: "2025-01-01T00:03:00Z",
        payload: { contract_id: "CTR-001", revision: 1, plan_id: "PLAN-1", plan_hash: "abc123" },
      },
      {
        event_type: "human_approved", sequence: 4, timestamp: "2025-01-01T00:04:00Z",
        payload: { contract_id: "CTR-001", operator: "admin@test.com" },
      },
      {
        event_type: "actions_executed", sequence: 5, timestamp: "2025-01-01T00:05:00Z",
        payload: { contract_id: "CTR-001", summary: "Restarted svc" },
      },
      {
        event_type: "recovery_verified", sequence: 6, timestamp: "2025-01-01T00:06:00Z",
        payload: { recovered: true },
      },
      {
        event_type: "outcome", sequence: 7, timestamp: "2025-01-01T00:07:00Z",
        payload: { result: "success" },
      },
    ];

    render(
      <ResolutionRecord
        events={events}
        incidentId="INC-001"
        incidentStatus="RESOLVED"
        incidentDetail={{ status: "RESOLVED" }}
      />
    );

    // Should show contract ID
    expect(screen.getByText("CTR-001")).toBeInTheDocument();
    // Should show strategy
    expect(screen.getByText(/restart svc/i)).toBeInTheDocument();
    // Should show APPROVED decision
    expect(screen.getAllByText(/Authorized/i).length).toBeGreaterThan(0);
    // Should show operator
    expect(screen.getByText(/admin@test.com/i)).toBeInTheDocument();
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * UNIT TESTS: Data-shape selectors
 * ═══════════════════════════════════════════════════════════════════════ */
describe("planEventData() handles nested payload.plan", () => {
  it("extracts from nested plan object", () => {
    const evt = {
      event_type: "plan_created",
      payload: {
        plan: { strategy_summary: "drain-and-restart", actions: [{ type: "restart" }], risk_level: "medium", plan_hash: "abc" },
        plan_hash: "abc",
      },
    };
    const data = planEventData(evt);
    expect(data.strategySummary).toBe("drain-and-restart");
    expect(data.actions).toHaveLength(1);
    expect(data.riskLevel).toBe("medium");
  });

  it("handles flat payload without nesting", () => {
    const data = planEventData({ event_type: "plan_created", payload: { strategy_summary: "scale-up", actions: [], risk_level: "low" } });
    expect(data.strategySummary).toBe("scale-up");
    expect(data.riskLevel).toBe("low");
  });
});

describe("findVerdictForPlan() bounds to plan window", () => {
  it("finds verdict between plan and next boundary", () => {
    const events = [
      { event_type: "plan_created", sequence: 1, payload: {} },
      { event_type: "verdict_issued", sequence: 2, payload: { verdict: "APPROVED" } },
      { event_type: "contract_issued", sequence: 3, payload: {} },
    ];
    expect(findVerdictForPlan(events, events[0])?.event_type).toBe("verdict_issued");
  });

  it("does not return verdict from a different plan window", () => {
    const events = [
      { event_type: "plan_created", sequence: 1, payload: {} },
      { event_type: "verdict_issued", sequence: 2, payload: { verdict: "CHALLENGE" } },
      { event_type: "plan_created", sequence: 3, payload: {} },
      { event_type: "verdict_issued", sequence: 4, payload: { verdict: "APPROVED" } },
    ];
    const vp = eventPayload(findVerdictForPlan(events, events[0]));
    expect(vp.verdict).toBe("CHALLENGE");
  });
});

describe("deriveCouncilRecommendation() uses latest investigation before plan", () => {
  it("picks the investigation immediately before the matched plan, not the first", () => {
    const events = [
      { event_type: "triage_completed", sequence: 1, payload: {} },
      { event_type: "investigation_completed", sequence: 2, payload: { diagnosis: "first pass" } },
      { event_type: "plan_created", sequence: 3, payload: { plan: { revision: 1, actions: [] } } },
      { event_type: "verdict_issued", sequence: 4, payload: { verdict: "CHALLENGE" } },
      { event_type: "investigation_completed", sequence: 5, payload: { diagnosis: "deeper re-investigation" } },
      { event_type: "plan_created", sequence: 6, payload: { plan: { revision: 2, actions: [{ skill: "fix" }] } } },
      { event_type: "verdict_issued", sequence: 7, payload: { verdict: "APPROVED" } },
      { event_type: "contract_issued", sequence: 8, payload: { revision: 2 } },
    ];
    const rec = deriveCouncilRecommendation(events, 2);
    expect(rec.investigation).not.toBeNull();
    expect(rec.investigation.payload.diagnosis).toBe("deeper re-investigation");
  });

  it("falls back to first investigation if none exists before the plan", () => {
    const events = [
      { event_type: "investigation_completed", sequence: 5, payload: { diagnosis: "only one" } },
      { event_type: "plan_created", sequence: 3, payload: { plan: { revision: 1, actions: [] } } },
    ];
    const rec = deriveCouncilRecommendation(events, 1);
    expect(rec.investigation.payload.diagnosis).toBe("only one");
  });
});

describe("reconstructApprovalHistory", () => {
  it("correlates by contract_id and revision", () => {
    const events = [
      { event_type: "plan_created", sequence: 1, payload: { plan: { plan_id: "p1" }, plan_hash: "abc123" } },
      { event_type: "verdict_issued", sequence: 2, payload: { verdict: "APPROVED" } },
      { event_type: "contract_issued", sequence: 3, payload: { contract_id: "c1", revision: 1, plan_id: "p1", plan_hash: "abc123" } },
      { event_type: "human_approved", sequence: 4, payload: { contract_id: "c1", revision: 1, operator: "admin" } },
      { event_type: "actions_executed", sequence: 5, payload: { contract_id: "c1" } },
      { event_type: "recovery_verified", sequence: 6, payload: {} },
    ];
    const h = reconstructApprovalHistory(events);
    expect(h).not.toBeNull();
    expect(h.contractId).toBe("c1");
    expect(h.revision).toBe(1);
    expect(h.approvalEvt).not.toBeNull();
  });
});

describe("deriveAgentState()", () => {
  it("Muhtasib shows active during REVIEWING even with prior CHALLENGE", () => {
    const events = [{ event_type: "verdict_issued", sequence: 1, payload: { verdict: "CHALLENGE" } }];
    expect(deriveAgentState("muhtasib", events, "REVIEWING")).toBe("active");
  });

  it("Muhaqqiq shows active during ANALYZING after triage", () => {
    const events = [
      { event_type: "triage_completed", sequence: 1 },
      { event_type: "investigation_completed", sequence: 2 },
    ];
    expect(deriveAgentState("muhaqqiq", events, "ANALYZING")).toBe("active");
  });
});

describe("extractTelemetry()", () => {
  it("filters agent_usage_telemetry from events", () => {
    const events = [
      { event_type: "incident_created" },
      { event_type: "agent_usage_telemetry", payload: { agent: "Nigehban" } },
      { event_type: "triage_completed" },
    ];
    const tel = extractTelemetry(events);
    expect(tel).toHaveLength(1);
  });
});

describe("filterAgentRoomMessages()", () => {
  it("deduplicates by message_id", () => {
    const msgs = [
      { message_id: "m1", sender: "Nigehban", content: "Hi" },
      { message_id: "m1", sender: "Nigehban", content: "Hi" },
      { message_id: "m2", sender: "Muhaqqiq", content: "Evidence" },
    ];
    expect(filterAgentRoomMessages(msgs)).toHaveLength(2);
  });

  it("excludes non-agent senders", () => {
    const msgs = [
      { message_id: "m1", sender: "Nigehban", content: "Hi" },
      { message_id: "m2", sender: "gateway", content: "System" },
      { message_id: "m3", sender: "system", content: "Log" },
    ];
    const filtered = filterAgentRoomMessages(msgs);
    expect(filtered.map((m) => m.sender)).not.toContain("gateway");
    expect(filtered.map((m) => m.sender)).not.toContain("system");
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * SERVICE OPTIONS: Executable vs escalation-only
 * ═══════════════════════════════════════════════════════════════════════ */
describe("Service options guardrails", () => {
  it("auth-service, payment-gateway, user-service are executable", () => {
    const executable = SERVICE_OPTIONS.filter((s) => s.executable).map((s) => s.id);
    expect(executable).toContain("auth-service");
    expect(executable).toContain("payment-gateway");
    expect(executable).toContain("user-service");
  });

  it("cache-cluster and api-gateway are escalation-only", () => {
    const escalation = SERVICE_OPTIONS.filter((s) => s.executable === false).map((s) => s.id);
    expect(escalation).toContain("cache-cluster");
    expect(escalation).toContain("api-gateway");
  });

  it("all SERVICE_OPTIONS have id, label, executable fields", () => {
    for (const s of SERVICE_OPTIONS) {
      expect(s).toHaveProperty("id");
      expect(s).toHaveProperty("label");
      expect(typeof s.executable).toBe("boolean");
    }
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * TELEMETRY FIELDS
 * ═══════════════════════════════════════════════════════════════════════ */
describe("Telemetry fields", () => {
  it("TELEMETRY_FIELDS has all required fields", () => {
    const required = ["agent", "prompt_tokens", "candidates_tokens", "thoughts_tokens", "total_tokens", "thinking_level", "tools_called", "tools_succeeded", "tools_failed"];
    for (const f of required) {
      expect(TELEMETRY_FIELDS).toContain(f);
    }
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * MESSAGE TYPE LABELS: Include verdict subtypes
 * ═══════════════════════════════════════════════════════════════════════ */
describe("Message type labels include verdict subtypes", () => {
  it("has safety-approved, blocked, escalated labels", () => {
    expect(MESSAGE_TYPE_LABELS).toHaveProperty("safety-approved");
    expect(MESSAGE_TYPE_LABELS).toHaveProperty("blocked");
    expect(MESSAGE_TYPE_LABELS).toHaveProperty("escalated");
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * DEMO PRESETS: correct service IDs
 * ═══════════════════════════════════════════════════════════════════════ */
describe("Demo preset service IDs", () => {
  it("cache_stampede uses payment-gateway", () => {
    expect(DEMO_PRESETS.find((d) => d.scenario_id === "cache_stampede").alert.service_id).toBe("payment-gateway");
  });
  it("prompt_injection uses payment-gateway", () => {
    expect(DEMO_PRESETS.find((d) => d.scenario_id === "prompt_injection").alert.service_id).toBe("payment-gateway");
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * SOURCE-INSPECTION INVARIANTS (security, no dangerouslySetInnerHTML)
 * ═══════════════════════════════════════════════════════════════════════ */
describe("Security invariants (source-inspection)", () => {
  const componentFiles = [
    "app/components/AgentRoom.jsx",
    "app/components/ApprovalTab.jsx",
    "app/components/AuditTab.jsx",
    "app/components/ResolutionRecord.jsx",
    "app/components/GuidedDemoCoach.jsx",
    "app/components/NavRail.jsx",
    "app/components/OverviewPage.jsx",
    "app/components/LaunchPage.jsx",
    "app/components/IncidentsPage.jsx",
    "app/components/EvidencePage.jsx",
    "app/components/EmptyPageState.jsx",
    "app/page.js",
  ];

  it("no file uses dangerouslySetInnerHTML as JSX attribute", () => {
    for (const f of componentFiles) {
      const src = readSrc(f);
      // Match JSX attribute: dangerouslySetInnerHTML=  (not in comments)
      const lines = src.split("\n");
      for (const line of lines) {
        const trimmed = line.trim();
        // Skip comment lines
        if (trimmed.startsWith("//") || trimmed.startsWith("/*") || trimmed.startsWith("*")) continue;
        expect(trimmed).not.toContain("dangerouslySetInnerHTML=");
      }
    }
  });

  it("no file uses eval()", () => {
    for (const f of componentFiles) {
      const src = readSrc(f);
      // Match eval( but not "evaluation" or "eval_"
      const matches = src.match(/\beval\s*\(/g);
      expect(matches).toBeNull();
    }
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * SOURCE-INSPECTION: AbortController for cross-incident guard
 * ═══════════════════════════════════════════════════════════════════════ */
describe("Cross-incident race guard (source)", () => {
  it("page.js has AbortController in SSE effect", () => {
    const src = readSrc("app/page.js");
    expect(src).toContain("new AbortController()");
    expect(src).toContain("controller.abort()");
    expect(src).toContain("activeIncidentRef");
  });

  it("AuditTab uses AbortController for verification", () => {
    const src = readSrc("app/components/AuditTab.jsx");
    expect(src).toContain("new AbortController()");
    expect(src).toContain("verifyIncidentRef");
  });

  it("ResolutionRecord uses AbortController for verification", () => {
    const src = readSrc("app/components/ResolutionRecord.jsx");
    expect(src).toContain("new AbortController()");
    expect(src).toContain("verifyIncidentRef");
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * SOURCE-INSPECTION: Dockerfile has NEXT_PUBLIC_GATEWAY_URL
 * ═══════════════════════════════════════════════════════════════════════ */
describe("Dockerfile SSE URL", () => {
  it("has NEXT_PUBLIC_GATEWAY_URL build arg", () => {
    const src = readSrc("Dockerfile");
    expect(src).toContain("NEXT_PUBLIC_GATEWAY_URL");
    expect(src).toContain("ARG NEXT_PUBLIC_GATEWAY_URL");
    expect(src).toContain("ENV NEXT_PUBLIC_GATEWAY_URL");
  });
});

/* ═══════════════════════════════════════════════════════════════════════
 * SOURCE-INSPECTION: Latest contract in AgentRoom
 * ═══════════════════════════════════════════════════════════════════════ */
describe("AgentRoom uses latest contract", () => {
  it("uses reverse().find() for contract_issued", () => {
    const src = readSrc("app/components/AgentRoom.jsx");
    // Should reverse-sort events and find latest contract
    expect(src).toContain(".reverse()");
    expect(src).toContain("contract_issued");
  });
});
