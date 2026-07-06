"use client";
/* ═══════════════════════════════════════════════════════════════════════════════
 * MuhafizSRE — Incident Command Room Dashboard (NavRail Orchestrator)
 * ═══════════════════════════════════════════════════════════════════════════════
 * Real-time incident response dashboard powered by Server-Sent Events.
 * Navigation Rail layout with 6 pages: Overview | Launch | Incidents |
 * Approvals | Agents & Room | Evidence
 *
 * This file is the orchestrator only. All UI components are imported from
 * dashboard/app/components/. Agent identities are in dashboard/app/personas.js.
 *
 * SSE events arrive as unnamed events. The data JSON contains a `type` field:
 * "event", "room_message", "stream_complete", or "heartbeat".
 * ═══════════════════════════════════════════════════════════════════════════════ */

import { useState, useEffect, useRef, useCallback, Fragment } from "react";

/* ── Imports from component library ─────────────────────────────────────── */
import {
  AGENT_PERSONAS,
  AGENT_ORDER,
  STATUS_PRIORITY,
  SEVERITY_OPTIONS,
  SERVICE_OPTIONS,
  TERMINAL_STATUSES,
} from "./personas";

import NavRail from "./components/NavRail";
import OverviewPage from "./components/OverviewPage";
import LaunchPage from "./components/LaunchPage";
import IncidentsPage from "./components/IncidentsPage";
import AgentRoom from "./components/AgentRoom";
import ApprovalTab from "./components/ApprovalTab";
import EvidencePage from "./components/EvidencePage";
import GuidedDemoCoach from "./components/GuidedDemoCoach";

/* ── Constants ────────────────────────────────────────────────────────────── */

const API_BASE = "/api";
const SSE_BASE =
  (typeof window !== "undefined" &&
    (process.env.NEXT_PUBLIC_GATEWAY_URL || window.location.origin)) + "/api";

/* ── Utility Functions ────────────────────────────────────────────────────── */

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

/* ── API helpers ──────────────────────────────────────────────────────────── */

async function apiFetch(path, opts = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: { "Content-Type": "application/json", ...opts.headers },
      ...opts,
      signal: opts.signal || controller.signal,
    });
    clearTimeout(timeout);
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`API ${res.status}: ${text}`);
    }
    return res.json();
  } catch (err) {
    clearTimeout(timeout);
    throw err;
  }
}

/* ═════════════════════════════════════════════════════════════════════════════
 * Component: RecordingRubricBar — Rubric proof strip for recording mode
 * ═════════════════════════════════════════════════════════════════════════════ */
function RecordingRubricBar() {
  return (
    <div className="recording-rubric-panel" aria-label="Rubric proof stack">
      <div className="recording-rubric-panel__title">Rubric Proof</div>
      <div className="recording-rubric-panel__chips">
        <span className="proof-chip proof-chip--azure">ADK Multi-agent</span>
        <span className="proof-chip proof-chip--violet">MCP Telemetry</span>
        <span className="proof-chip proof-chip--emerald">Gemini 3-tier</span>
        <span className="proof-chip proof-chip--amber">HMAC Approval</span>
        <span className="proof-chip proof-chip--teal">Hash-Chain Audit</span>
        <span className="proof-chip proof-chip--gray">Cloud Run Live</span>
      </div>
    </div>
  );
}

/* ═════════════════════════════════════════════════════════════════════════════
 * Component: Dashboard (default export) — Main orchestrator
 * ═════════════════════════════════════════════════════════════════════════════ */
export default function Dashboard() {
  /* ── State ── */
  const [incidents, setIncidents] = useState([]);
  const [activeIncident, setActiveIncident] = useState(null);
  const [events, setEvents] = useState([]);
  const [roomMessages, setRoomMessages] = useState([]);
  const [contract, setContract] = useState(null);
  const [approvalToken, setApprovalToken] = useState("");
  const [connectionStatus, setConnectionStatus] = useState("idle");
  const [incidentDetail, setIncidentDetail] = useState(null);
  const [activePage, setActivePage] = useState("overview");
  const [isRecordingMode, setIsRecordingMode] = useState(false);
  const [guidedDemo, setGuidedDemo] = useState(null); // { incidentId, scenarioId } | null
  const [expiryMap, setExpiryMap] = useState({});
  const [evalData, setEvalData] = useState(null);

  /* ── Recording mode: ?recording=true hides sidebar, enlarges key text (Item 18) ── */
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const recording = params.get("recording") === "true";
    setIsRecordingMode(recording);
    if (recording) {
      document.body.classList.add("recording-mode");
    } else {
      document.body.classList.remove("recording-mode");
    }
    return () => document.body.classList.remove("recording-mode");
  }, []);

  /* ── Load static evaluation summary ── */
  useEffect(() => {
    fetch("/evaluation-summary.json")
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(setEvalData)
      .catch(() => setEvalData("unavailable"));
  }, []);

  const eventSourceRef = useRef(null);
  const lastEventIdRef = useRef(null);
  const refreshTimerRef = useRef(null);
  const terminalStreamRef = useRef(false);
  const didInitialAutoSelectRef = useRef(false);
  const streamGenerationRef = useRef(0);
  const activeIncidentRef = useRef(null);

  /* ── Hydrate expiry map for sidebar badges ── */
  const hydrateExpiryMap = useCallback(async (list) => {
    const awaiting = list.filter((inc) => inc.status === "AWAITING_APPROVAL");
    if (!awaiting.length) return;
    const results = await Promise.allSettled(
      awaiting.map((inc) =>
        apiFetch(`/incidents/${inc.incident_id || inc.id}/contract`)
      )
    );
    const next = {};
    results.forEach((result, i) => {
      if (result.status === "fulfilled") {
        const c = result.value?.contract || result.value;
        const id = awaiting[i].incident_id || awaiting[i].id;
        next[id] = c?.expires_at || c?.token_expires_at || null;
      }
    });
    setExpiryMap((prev) => ({ ...prev, ...next }));
  }, []);

  /* ── Fetch all incidents ── */
  const fetchIncidents = useCallback(async () => {
    try {
      const data = await apiFetch("/incidents");
      const list = Array.isArray(data) ? data : data.incidents || [];
      setIncidents(list);
      hydrateExpiryMap(list);
    } catch {
      // Silently retry on next interval
    }
  }, [hydrateExpiryMap]);

  /* ── Fetch single incident detail ── */
  const fetchIncidentDetail = useCallback(async (id, { signal, generation } = {}) => {
    try {
      const data = await apiFetch(`/incidents/${id}`, { signal });
      // Guard: discard if incident or generation has changed
      if (signal?.aborted) return null;
      if (generation != null && generation !== streamGenerationRef.current) return null;
      if (id !== activeIncidentRef.current) return null;
      setIncidentDetail(data);
      return data;
    } catch {
      return null;
    }
  }, []);

  /* ── Fetch contract (for approval gate) ── */
  const fetchContract = useCallback(async (id, { signal, generation } = {}) => {
    try {
      const data = await apiFetch(`/incidents/${id}/contract`, { signal });
      // Guard: discard if incident or generation has changed
      if (signal?.aborted) return;
      if (generation != null && generation !== streamGenerationRef.current) return;
      if (id !== activeIncidentRef.current) return;
      setContract(data?.contract ?? null);
      setApprovalToken(data?.approval_token ?? "");
    } catch {
      if (signal?.aborted) return;
      setContract(null);
      setApprovalToken("");
    }
  }, []);

  /* ── Initial load + auto-refresh ── */
  useEffect(() => {
    async function loadAndAutoSelect() {
      try {
        const data = await apiFetch("/incidents");
        const list = Array.isArray(data) ? data : data.incidents || [];
        setIncidents(list);
        hydrateExpiryMap(list);

        // Auto-select: pick the highest-priority active incident
        // Only on first load (didInitialAutoSelectRef guards against re-runs)
        if (!didInitialAutoSelectRef.current && list.length > 0) {
          didInitialAutoSelectRef.current = true;
          let best = null;
          let bestPrio = -1;
          let bestStatus = "";
          for (const inc of list) {
            const prio = STATUS_PRIORITY.indexOf(inc.status);
            if (prio > bestPrio) {
              bestPrio = prio;
              best = inc.incident_id || inc.id;
              bestStatus = inc.status;
            }
          }
          if (best) {
            lastEventIdRef.current = null;
            setActiveIncident(best);
            // Stay on overview — judges see the landing page first.
            // Incident is silently pre-selected for when they navigate.
          }
        } else if (list.length === 0) {
          // No incidents — show overview
          setActivePage("overview");
        }
      } catch {
        // Backend unreachable — show welcome page with empty state
        setIncidents([]);
        setActivePage("overview");
      }
    }
    loadAndAutoSelect();
    refreshTimerRef.current = setInterval(fetchIncidents, 5000);
    return () => {
      if (refreshTimerRef.current) clearInterval(refreshTimerRef.current);
    };
  }, [fetchIncidents, hydrateExpiryMap]);

  /* ── SSE Connection (generation-guarded) ── */
  useEffect(() => {
    if (!activeIncident) {
      setConnectionStatus("idle");
      return;
    }

    // Increment generation — invalidates all prior async work
    const generation = ++streamGenerationRef.current;
    activeIncidentRef.current = activeIncident;
    let cancelled = false;
    const controller = new AbortController();
    const signal = controller.signal;

    const isCurrent = () =>
      !cancelled && generation === streamGenerationRef.current;

    // Cleanup previous connection
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }

    setEvents([]);
    setRoomMessages([]);
    setContract(null);
    setApprovalToken("");
    setConnectionStatus("connecting");
    terminalStreamRef.current = false;

    // ── Step 1: Hydrate existing history via REST ──
    async function hydrateIncident(id) {
      try {
        const roomData = await apiFetch(`/incidents/${id}/room`, { signal });
        if (!isCurrent()) return;
        const hydratedRoomMsgs = (roomData.messages || []).map((m) => ({
          ...m,
          actor: m.sender,
          msg_type: m.message_type,
          timestamp: m.timestamp || m.created_at,
          _source: "room",
        }));
        setRoomMessages(hydratedRoomMsgs);
      } catch {
        // Hydration failed; SSE will still deliver everything
      }
    }

    // ── Step 2: Open SSE for incremental live updates ──
    async function connectSSE(id) {
      await hydrateIncident(id);
      if (!isCurrent()) return;

      let url = `${SSE_BASE}/incidents/${id}/events`;
      if (lastEventIdRef.current) {
        url += `?last_event_id=${encodeURIComponent(
          lastEventIdRef.current
        )}`;
      }

      const es = new EventSource(url);

      // Double-check after construction — user may have switched
      if (!isCurrent()) {
        es.close();
        return;
      }

      eventSourceRef.current = es;

      es.onopen = () => {
        if (!isCurrent()) return;
        setConnectionStatus("connected");
      };

      es.onmessage = (msg) => {
        if (!isCurrent()) return;
        try {
          const data = JSON.parse(msg.data);

          // Timestamp normalizer
          if (data.created_at && !data.timestamp) {
            data.timestamp = data.created_at;
          }

          // Track Last-Event-ID for reconnection
          if (msg.lastEventId) {
            lastEventIdRef.current = msg.lastEventId;
          } else if (data.event_id) {
            lastEventIdRef.current = data.event_id;
          }

          const msgType = data.type || "event";

          if (msgType === "heartbeat") return;

          if (msgType === "stream_complete") {
            terminalStreamRef.current = true;
            es.close();
            eventSourceRef.current = null;
            setConnectionStatus("complete");
            if (isCurrent()) {
              fetchIncidentDetail(activeIncident, { signal, generation });
              fetchIncidents();
            }
            return;
          }

          if (msgType === "room_message") {
            setRoomMessages((prev) => {
              const id = data.message_id || data.event_id;
              if (
                id &&
                prev.some(
                  (m) => (m.message_id || m.event_id) === id
                )
              ) {
                return prev;
              }
              return [
                ...prev,
                {
                  ...data,
                  actor: data.sender || data.actor,
                  _source: "room",
                },
              ];
            });
            return;
          }

          // event
          setEvents((prev) => {
            const evtId =
              data.event_hash ||
              `${data.incident_id}-${data.sequence}`;
            if (
              evtId &&
              prev.some(
                (e) =>
                  (e.event_hash ||
                    `${e.incident_id}-${e.sequence}`) === evtId
              )
            ) {
              return prev;
            }
            return [...prev, { ...data, _source: "event" }];
          });

          // Fetch contract only on contract_issued — a challenge is not a contract
          if (data.event_type === "contract_issued") {
            fetchContract(activeIncident, { signal, generation });
            setActivePage("approvals");
          }

          if (isCurrent()) {
            fetchIncidentDetail(activeIncident, { signal, generation });
            fetchIncidents();
          }
        } catch {
          // Skip malformed events
        }
      };

      es.onerror = () => {
        if (!isCurrent()) return;
        if (terminalStreamRef.current) return;

        setConnectionStatus("reconnecting");
        es.close();
        eventSourceRef.current = null;
        // Auto-reconnect after 3s
        setTimeout(() => {
          if (!isCurrent()) return;
          connectSSE(activeIncident);
        }, 3000);
      };
    }

    connectSSE(activeIncident);
    fetchIncidentDetail(activeIncident, { signal, generation });
    fetchContract(activeIncident, { signal, generation });

    // ── Room polling fallback: reconcile every 5s while incident is active ──
    // Ensures room messages and status stay current even if SSE stalls
    const roomPollInterval = setInterval(async () => {
      if (!isCurrent()) return;
      if (terminalStreamRef.current) return;
      try {
        // Re-fetch room messages and merge/dedupe
        const roomData = await apiFetch(`/incidents/${activeIncident}/room`, { signal });
        if (!isCurrent()) return;
        const freshMsgs = (roomData.messages || []).map((m) => ({
          ...m,
          actor: m.sender,
          msg_type: m.message_type,
          timestamp: m.timestamp || m.created_at,
          _source: "room",
        }));
        setRoomMessages((prev) => {
          const existingIds = new Set(prev.map((m) => m.message_id || m.event_id));
          const newMsgs = freshMsgs.filter(
            (m) => !(existingIds.has(m.message_id || m.event_id))
          );
          return newMsgs.length > 0 ? [...prev, ...newMsgs] : prev;
        });
        // Re-fetch events and merge/dedupe
        const eventsData = await apiFetch(`/incidents/${activeIncident}/events/list`, { signal });
        if (!isCurrent()) return;
        const freshEvents = (eventsData.events || []).map((e) => ({
          ...e,
          timestamp: e.created_at,
          _source: "event",
        }));
        setEvents((prev) => {
          const existingIds = new Set(
            prev.map((e) => e.event_hash || `${e.incident_id}-${e.sequence}`)
          );
          const newEvts = freshEvents.filter(
            (e) => !(existingIds.has(e.event_hash || `${e.incident_id}-${e.sequence}`))
          );
          return newEvts.length > 0 ? [...prev, ...newEvts] : prev;
        });
        // Also refresh incident detail + contract to keep status current
        fetchIncidentDetail(activeIncident, { signal, generation });
        fetchContract(activeIncident, { signal, generation });
      } catch {
        // Polling failure is non-fatal — SSE may still be delivering
      }
    }, 5000);

    return () => {
      cancelled = true;
      controller.abort();
      streamGenerationRef.current += 1;
      activeIncidentRef.current = null;
      clearInterval(roomPollInterval);
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
    };
  }, [activeIncident, fetchContract, fetchIncidentDetail, fetchIncidents]);

  /* ── Select incident (status-aware routing) ── */
  const handleSelectIncident = useCallback(
    (id) => {
      lastEventIdRef.current = null;
      terminalStreamRef.current = false;
      // Only clear guided demo if switching to a DIFFERENT incident
      setGuidedDemo((prev) =>
        prev && prev.incidentId === id ? prev : null
      );
      setActiveIncident(id);
      const inc = incidents.find(
        (i) => (i.incident_id || i.id) === id
      );
      if (inc && inc.status === "AWAITING_APPROVAL") {
        setActivePage("approvals");
      } else {
        setActivePage("agents");
      }
    },
    [incidents]
  );

  /* ── Logo click navigation ── */
  const handleLogoClick = useCallback(() => {
    setActivePage("overview");
  }, []);

  /* ── Incident created callback ── */
  const handleIncidentCreated = useCallback(
    (result, scenarioId = null) => {
      fetchIncidents();
      const id = result?.incident_id || result?.id;
      if (id) {
        lastEventIdRef.current = null;
        terminalStreamRef.current = false;
        setActiveIncident(id);
        if (scenarioId) {
          setGuidedDemo({ incidentId: id, scenarioId });
        } else {
          setGuidedDemo(null);
        }
        setActivePage("agents");
      }
    },
    [fetchIncidents]
  );

  /* ── Decision submitted callback ── */
  const handleDecisionSubmitted = useCallback(
    (action) => {
      setContract(null);
      setApprovalToken("");
      if (activeIncident) {
        fetchIncidentDetail(activeIncident);
        fetchIncidents();
      }
      setActivePage("agents");
    },
    [activeIncident, fetchIncidentDetail, fetchIncidents]
  );

  /* ── GuidedDemoCoach tab adapter ── */
  const TAB_TO_PAGE = { room: "agents", approval: "approvals", audit: "evidence" };
  const PAGE_TO_TAB = { agents: "room", approvals: "approval", evidence: "audit" };
  const handleCoachNav = useCallback((tabId) => {
    setActivePage(TAB_TO_PAGE[tabId] || tabId);
  }, []);

  /* ── Derive incident status ── */
  const currentStatus = incidentDetail?.status || "";
  const hasAwaitingApproval = incidents.some((i) => i.status === "AWAITING_APPROVAL");

  /* ── Render ── */
  return (
    <div className="dashboard">
      {/* ── NavRail — permanent left sidebar ── */}
      <NavRail
        activePage={activePage}
        onNavigate={setActivePage}
        incidentCount={incidents.length}
        awaitingApproval={hasAwaitingApproval}
        evalData={evalData}
        connectionStatus={connectionStatus}
      />

      {/* ── Content Area ── */}
      <main className="content-area">
        {/* Recording mode rubric bar */}
        {isRecordingMode && <RecordingRubricBar />}

        {/* Guided Demo Coach — scoped to created incident */}
        {guidedDemo?.incidentId === activeIncident && (
          <GuidedDemoCoach
            incidentId={activeIncident}
            events={events}
            incidentStatus={currentStatus}
            onSwitchTab={handleCoachNav}
            demoScenarioId={guidedDemo.scenarioId}
            activeTab={PAGE_TO_TAB[activePage]}
          />
        )}

        {/* ── Page Router ── */}
        {activePage === "overview" && (
          <OverviewPage onNavigate={setActivePage} />
        )}

        {activePage === "launch" && (
          <LaunchPage
            incidents={incidents}
            onCreateIncident={handleIncidentCreated}
            onSelectIncident={handleSelectIncident}
          />
        )}

        {activePage === "incidents" && (
          <IncidentsPage
            incidents={incidents}
            activeIncidentId={activeIncident}
            onSelect={handleSelectIncident}
            expiryMap={expiryMap}
            onNavigate={setActivePage}
          />
        )}

        {activePage === "approvals" && (
          activeIncident ? (
            <ApprovalTab
              contract={contract}
              approvalToken={approvalToken}
              incidentId={activeIncident}
              incidentStatus={currentStatus}
              events={events}
              onDecisionSubmitted={handleDecisionSubmitted}
              incidentDetail={incidentDetail}
            />
          ) : (
            <div className="vault-empty">
              <div className="vault-empty__lock">🔒</div>
              <h2 className="vault-empty__title">Authorization Vault</h2>
              <p className="vault-empty__subtitle">No contract awaiting authorization.</p>
              <div className="vault-empty__door">
                <p className="vault-empty__hint">
                  When agents prepare a remediation plan, the HMAC-signed contract
                  appears here for your review and authorization.
                </p>
              </div>
              <button className="vault-empty__cta" onClick={() => setActivePage("launch")}>
                ▶ Launch Guided Incident
              </button>
            </div>
          )
        )}

        {activePage === "agents" && (
          activeIncident ? (
            <AgentRoom
              roomMessages={roomMessages}
              events={events}
              incidentStatus={currentStatus}
            />
          ) : (
            <div className="chamber-empty">
              <div className="page-hero">
                <span className="page-hero__icon">🤖</span>
                <h2 className="page-hero__title">Council Chamber</h2>
                <p className="page-hero__subtitle">Five Gemini agents investigate and challenge the plan.</p>
              </div>
              <div className="chamber-formation">
                {AGENT_ORDER.map((agentId, i) => {
                  const agent = AGENT_PERSONAS[agentId];
                  return (
                    <Fragment key={agentId}>
                      {i > 0 && <span className="chamber-formation__arrow">→</span>}
                      <div className="chamber-formation__agent">
                        <img
                          src={agent.portrait}
                          alt={agent.displayName}
                          className="chamber-formation__portrait"
                          onError={(e) => { e.currentTarget.onerror = null; e.currentTarget.src = agent.fallbackAvatar; }}
                        />
                        <span className="chamber-formation__name">{agent.displayName}</span>
                        <span className="chamber-formation__role">{agent.shortRole}</span>
                        <span className="chamber-formation__ready">● Ready</span>
                      </div>
                    </Fragment>
                  );
                })}
              </div>
              <button className="chamber-empty__cta" onClick={() => setActivePage("launch")}>
                ▶ Convene the Council
              </button>
            </div>
          )
        )}

        {activePage === "evidence" && (
          <EvidencePage
            evalData={evalData}
            events={events}
            incidentId={activeIncident}
            onNavigate={setActivePage}
          />
        )}
      </main>
    </div>
  );
}

