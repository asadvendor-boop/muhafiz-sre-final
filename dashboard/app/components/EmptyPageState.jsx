"use client";
/* EmptyPageState — Designed story card for incident-dependent pages */

export default function EmptyPageState({ title, message, icon, onNavigate, target }) {
  return (
    <div className="empty-page-state">
      <div className="empty-page-state__card">
        <div className="empty-page-state__icon">{icon || "📭"}</div>
        <h2 className="empty-page-state__title">{title}</h2>
        <p className="empty-page-state__message">{message}</p>
        {onNavigate && target && (
          <button className="empty-page-state__cta" onClick={() => onNavigate(target)}>
            {target === "launch" ? "▶ Start a Guided Incident" : "View Incidents"}
          </button>
        )}
      </div>
    </div>
  );
}
