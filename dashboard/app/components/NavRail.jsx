"use client";
/* NavRail — Permanent navigation sidebar */
import Link from "next/link";

export default function NavRail({ activePage, onNavigate, incidentCount, awaitingApproval, evalData, connectionStatus }) {
  const NAV_ITEMS = [
    { id: "overview", label: "Overview", icon: "📋" },
    { id: "launch", label: "Launch", icon: "🚀" },
    { id: "incidents", label: "Incidents", icon: "🔔" },
    { id: "approvals", label: "Approvals", icon: "✅" },
    { id: "agents", label: "Agents & Room", icon: "🤖" },
    { id: "evidence", label: "Evidence", icon: "📊" },
  ];

  const isLive = connectionStatus === "connected";
  const evalOk = evalData && evalData !== "unavailable";
  const passText = evalOk ? `${evalData.workflows_passed}/${evalData.total_workflows}` : "—/—";
  const unauthText = evalOk ? evalData.unauthorized_executions : "—";

  return (
    <nav className="nav-rail">
      <div className="nav-rail__logo" onClick={() => onNavigate("overview")} style={{ cursor: "pointer" }}>
        <img src="/muhafiz-logo.jpg" alt="MuhafizSRE" className="nav-rail__logo-img" />
        <div className="nav-rail__logo-text">
          <span className="nav-rail__title">MuhafizSRE</span>
          <span className="nav-rail__tagline">Google ADK × Gemini</span>
        </div>
      </div>

      <div className="nav-rail__nav">
        {NAV_ITEMS.map((item) => {
          const isActive = activePage === item.id;
          let badge = null;
          if (item.id === "incidents" && incidentCount > 0) badge = incidentCount;
          if (item.id === "approvals" && awaitingApproval) badge = "!";

          return (
            <button
              key={item.id}
              className={`nav-rail__item ${isActive ? "nav-rail__item--active" : ""} ${item.id === "approvals" && awaitingApproval ? "nav-rail__item--alert" : ""}`}
              onClick={() => onNavigate(item.id)}
              aria-current={isActive ? "page" : undefined}
            >
              <span className="nav-rail__icon">{item.icon}</span>
              <span className="nav-rail__label">{item.label}</span>
              {badge && (
                <span className={`nav-rail__badge ${item.id === "approvals" && awaitingApproval ? "nav-rail__badge--alert" : ""}`}>
                  {badge}
                </span>
              )}
            </button>
          );
        })}

        {/* Benchmark — separate Next.js route, not a client-side tab */}
        <Link href="/benchmark" className="nav-rail__item">
          <span className="nav-rail__icon">🔬</span>
          <span className="nav-rail__label">Benchmark</span>
        </Link>
      </div>

      <div className="nav-rail__status">
        <div className={`nav-rail__status-dot nav-rail__status-dot--${connectionStatus}`} />
        <span className="nav-rail__status-line">
          {isLive ? (process.env.NEXT_PUBLIC_DEPLOY_ENV === "cloudrun" ? "Cloud Run Live" : "Sandbox Live") : connectionStatus === "complete" ? "Run Complete" : connectionStatus === "idle" ? "Idle" : connectionStatus === "disconnected" ? "Disconnected" : "Connecting…"}
        </span>
        <span className="nav-rail__status-line">5/5 agents ready</span>
        <span className="nav-rail__status-line">{passText} eval benchmark</span>
        <span className="nav-rail__status-line">{unauthText} unauthorized</span>
        {connectionStatus !== "idle" && connectionStatus !== "disconnected" && (
          <span className="nav-rail__status-line nav-rail__status-stream">
            Stream: {connectionStatus === "connected" ? "● Live" : connectionStatus === "complete" ? "✓ Complete" : "◌ Connecting…"}
          </span>
        )}
      </div>
    </nav>
  );
}

