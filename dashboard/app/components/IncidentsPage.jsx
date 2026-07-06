"use client";

/* ── Local helper (from page.js) ── */
function relativeTime(iso) {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return hrs < 24 ? `${hrs}h ago` : `${Math.floor(hrs / 24)}d ago`;
}

export default function IncidentsPage({
  incidents,
  activeIncidentId,
  onSelect,
  expiryMap = {},
  onNavigate,
}) {
  /* ── Empty state ── */
  if (!incidents || incidents.length === 0) {
    return (
      <div className="incidents-page">
        <div className="page-hero">
          <span className="page-hero__icon">📋</span>
          <h2 className="page-hero__title">Live Docket</h2>
          <p className="page-hero__subtitle">The live docket records every run.</p>
        </div>
        <div className="docket-readiness">
          <div className="docket-readiness__card">
            <span className="docket-readiness__icon">📭</span>
            <h3 className="docket-readiness__heading">No active incidents</h3>
            <p className="docket-readiness__text">Launch a guided scenario to begin</p>
          </div>
          <div className="docket-readiness__card">
            <span className="docket-readiness__icon">📊</span>
            <h3 className="docket-readiness__heading">Benchmark ready</h3>
            <p className="docket-readiness__text">21/21 workflows verified</p>
          </div>
          <div className="docket-readiness__card">
            <span className="docket-readiness__icon">🤖</span>
            <h3 className="docket-readiness__heading">Council standing by</h3>
            <p className="docket-readiness__text">5 agents ready</p>
          </div>
          <div className="docket-readiness__card docket-readiness__card--cta">
            <span className="docket-readiness__icon">🚀</span>
            <h3 className="docket-readiness__heading">Ready to launch</h3>
            <button
              className="docket-readiness__cta"
              onClick={() => onNavigate("launch")}
            >
              ▶ Launch Guided Incident
            </button>
          </div>
        </div>
      </div>
    );
  }

  /* ── Populated list ── */
  return (
    <div className="incidents-page">
      <div className="page-hero" style={{ marginBottom: 20 }}>
        <span className="page-hero__icon">📋</span>
        <h2 className="page-hero__title">Live Docket</h2>
        <p className="page-hero__subtitle">
          {incidents.length} incident{incidents.length !== 1 ? "s" : ""} recorded
        </p>
      </div>

      <ul className="incident-list">
        {incidents.map((inc) => {
          const id = inc.incident_id || inc.id;
          const isActive = id === activeIncidentId;
          const sev = inc.severity || "P3";
          const statusVal = inc.status || "DETECTED";
          const summaryText = inc.summary || inc.alert?.summary || id;
          const serviceId = inc.service_id || inc.alert?.service_id || "";
          const needsAction = statusVal === "AWAITING_APPROVAL";

          return (
            <li
              key={id}
              className={`incident-item ${
                isActive ? "incident-item--active" : ""
              }`}
            >
              <button
                className="incident-item__btn"
                onClick={() => onSelect(id)}
                aria-label={`${sev} ${statusVal.replace(/_/g, " ")} incident ${id} on ${serviceId || "service"}`}
                aria-current={isActive ? "true" : undefined}
              >
                <span className={`incident-item__severity severity--${sev}`}>
                  {sev}
                </span>
                <div className="incident-item__info">
                  <div className="incident-item__id">
                    {id}
                    {serviceId && (
                      <span className="incident-item__service">
                        {" "}
                        · {serviceId}
                      </span>
                    )}
                  </div>
                  <div className="incident-item__summary">{summaryText}</div>
                  <div className="incident-item__time">
                    {relativeTime(inc.created_at)}
                  </div>
                </div>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "flex-end",
                    gap: 4,
                  }}
                >
                  <span
                    className={`incident-item__status status--${statusVal}`}
                  >
                    {statusVal.replace(/_/g, " ")}
                  </span>
                  {(() => {
                    const expiresAt = expiryMap[id];
                    const expired =
                      statusVal === "AWAITING_APPROVAL" &&
                      expiresAt &&
                      Date.now() > new Date(expiresAt).getTime();
                    if (expired) {
                      return (
                        <span className="incident-item__expired-badge">
                          Contract expired
                        </span>
                      );
                    }
                    if (needsAction) {
                      return (
                        <span className="incident-item__action-badge">
                          Action required
                        </span>
                      );
                    }
                    return null;
                  })()}
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
