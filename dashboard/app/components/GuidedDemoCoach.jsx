"use client";
/* ═══════════════════════════════════════════════════════════════════════════════
 * GuidedDemoCoach — 6-step incident-scoped demo walkthrough
 * ═══════════════════════════════════════════════════════════════════════════════
 * Submits { scenario_id: "cache_stampede", alert: { ... } }.
 * State scoped to { incidentId, step }.
 * Does NOT automate approval — coach points user to Approval tab.
 * ═══════════════════════════════════════════════════════════════════════════════ */

import { useState, useEffect } from "react";

const DEMO_STEPS = [
  {
    id: 1,
    title: "Incident Detected",
    description: "Nigehban receives the alert and begins triage. Watch the Agent Room as she confirms signal quality and severity.",
    tab: "room",
    watch: "triage_completed",
  },
  {
    id: 2,
    title: "Investigation",
    description: "Muhaqqiq examines metrics, logs, and traces to build an evidence-based diagnosis.",
    tab: "room",
    watch: "investigation_completed",
  },
  {
    id: 3,
    title: "Plan Created",
    description: "Mudabbir creates a bounded remediation plan with exact actions, rollback paths, and blast-radius limits.",
    tab: "room",
    watch: "plan_created",
  },
  {
    id: 4,
    title: "Safety Review",
    description: "Muhtasib challenges the plan adversarially. If it fails, Mudabbir revises. The cycle continues until Muhtasib approves and a contract is issued.",
    tab: "room",
    watch: "contract_issued",
  },
  {
    id: 5,
    title: "Operator Authorization Gate",
    description: "The plan is now awaiting YOUR approval. Switch to Approvals to review the exact contract, actions, and risk level. You decide.",
    tab: "approval",
    watch: "human_approved",
  },
  {
    id: 6,
    title: "Execution & Recovery",
    description: "After your approval, Aamil executes the exact authorized actions and verifies recovery. View the Resolution Record in Approvals.",
    tab: "approval",
    watch: "recovery_verified",
  },
];

export default function GuidedDemoCoach({
  incidentId,
  events,
  incidentStatus,
  onSwitchTab,
  demoScenarioId,
  activeTab,
}) {
  const [currentStep, setCurrentStep] = useState(0);
  const [dismissed, setDismissed] = useState(false);

  // Reset when incident changes
  useEffect(() => {
    setCurrentStep(0);
    setDismissed(false);
  }, [incidentId]);

  // Only show for demo scenarios
  if (!demoScenarioId || dismissed) return null;

  // Auto-advance based on events
  const eventTypes = new Set((events || []).map((e) => e.event_type));
  let advancedStep = 0;
  for (let i = 0; i < DEMO_STEPS.length; i++) {
    if (eventTypes.has(DEMO_STEPS[i].watch)) {
      advancedStep = i + 1;
    }
  }
  // Fallback: derive progress from incidentStatus when SSE events are stale
  const STATUS_TO_MIN_STEP = {
    ANALYZING: 1,
    PLANNING: 2,
    SAFETY_REVIEW: 3,
    AWAITING_APPROVAL: 4,
    EXECUTING: 5,
    RECOVERY_VERIFIED: 6,
    RESOLVED: 6,
  };
  const statusStep = STATUS_TO_MIN_STEP[incidentStatus] || 0;
  const displayStep = Math.max(currentStep, advancedStep, statusStep);
  const step = DEMO_STEPS[Math.min(displayStep, DEMO_STEPS.length - 1)];
  const isComplete = displayStep >= DEMO_STEPS.length;

  return (
    <div className="demo-coach">
      <div className="demo-coach__header">
        <span className="demo-coach__badge">Guided Demo</span>
        <span className="demo-coach__progress">
          Step {Math.min(displayStep + 1, DEMO_STEPS.length)} of{" "}
          {DEMO_STEPS.length}
        </span>
        <button
          className="demo-coach__dismiss"
          onClick={() => setDismissed(true)}
          aria-label="Dismiss demo coach"
        >
          CLOSE
        </button>
      </div>
      {isComplete ? (
        <div className="demo-coach__body">
          <div className="demo-coach__title">Demo Complete</div>
          <p className="demo-coach__desc">
            You've seen the full MuhafizSRE lifecycle: triage, investigation,
            planning, adversarial safety review, operator authorization, and verified
            execution. Every step is recorded in a tamper-evident audit chain.
          </p>
        </div>
      ) : (
        <div className="demo-coach__body">
          <div className="demo-coach__title">
            {step.id}. {step.title}
          </div>
          <p className="demo-coach__desc">{step.description}</p>
          {step.tab && onSwitchTab && step.tab !== activeTab && (
            <button
              className="demo-coach__tab-btn"
              onClick={() => onSwitchTab(step.tab)}
            >
              Open {step.tab === "room" ? "Agent Room" : "Approvals"}
            </button>
          )}
        </div>
      )}
      {/* Progress dots */}
      <div className="demo-coach__dots">
        {DEMO_STEPS.map((s, i) => (
          <span
            key={i}
            className={`demo-coach__dot ${
              i < displayStep
                ? "demo-coach__dot--done"
                : i === displayStep
                ? "demo-coach__dot--active"
                : ""
            }`}
          />
        ))}
      </div>
    </div>
  );
}
