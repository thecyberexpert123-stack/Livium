import React, { useMemo, useState } from "react";
import { Failure } from "./common.jsx";
import { Icon } from "./icons.jsx";

function groupOf(value) {
  if (!value) return "Earlier";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Earlier";
  const today = new Date();
  const midnight = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  if (date.getTime() >= midnight) return "Today";
  if (date.getTime() >= midnight - 86400000) return "Yesterday";
  return "Earlier";
}

function statusOf(task) {
  if (task.run?.running || task.status === "active") return "running";
  if (task.verification?.startsWith("verified")) return "verified";
  if (["blocked", "failed", "awaiting_approval"].includes(task.status)) return "blocked";
  return "unverified";
}

export function Sidebar({ tasks, selectedId, state, error, onSelect, onNew,
                          onRefresh, onOpenSheet, theme, onTheme, onHide }) {
  const [filter, setFilter] = useState("");
  const shown = useMemo(() => tasks.filter(task =>
    `${task.objective ?? ""} ${task.task_id ?? ""}`.toLowerCase().includes(filter.trim().toLowerCase())),
  [tasks, filter]);
  const groups = ["Today", "Yesterday", "Earlier"].map(label => ({
    label, items: shown.filter(task => groupOf(task.created_at) === label),
  })).filter(group => group.items.length);
  const running = state?.backend?.running;
  const reachable = state?.provider?.ok === true;
  const isDouble = state?.provider?.is_test_double === true || state?.settings?.provider === "scripted";
  const health = !running ? "Runtime offline" : state?.provider?.ok == null
    ? "Checking provider" : !reachable ? "Provider unavailable"
    : isDouble ? "Test double active" : "Ready to chat";

  return <aside className="sidebar" aria-label="Tasks and navigation">
    <div className="sidebar-brand">
      <div className="brand-lockup"><span className="brand-mark"><Icon name="sparkles" size={21} /></span>
        <span className="brand-name">livium<span className="brand-suffix"> AI</span>
          <small>YOUR AGENT SPACE</small></span></div>
      <button className="icon-btn sidebar-collapse" type="button" title="Hide the sidebar"
        aria-label="Hide the sidebar" onClick={onHide}><Icon name="sidebar" /></button>
    </div>

    <div className="sidebar-actions">
      <button className="new-task" type="button" onClick={onNew}>
        <Icon name="plus" size={18} /><span>New chat</span>
      </button>
      <label className="history-search"><Icon name="search" size={17} />
        <input aria-label="Filter recent tasks" placeholder="Search recent tasks"
          type="search" value={filter} onChange={event => setFilter(event.target.value)} />
      </label>
    </div>

    <div className="sidebar-scroll">
      <div className="sidebar-label"><span>RECENT TASKS <span className="sidebar-count">{tasks.length}</span></span>
        <button className="icon-btn" type="button" aria-label="Refresh task history"
          title="Refresh task history" onClick={onRefresh}><Icon name="refresh" size={16} /></button></div>
      <Failure error={error} context="Task history unavailable" />
      <div className="task-list" aria-label="Task history">
        {!error && !tasks.length && <p className="empty sidebar-empty">No conversations yet.<br />
          Start with a question or a task.</p>}
        {!error && tasks.length > 0 && !shown.length && <p className="empty sidebar-empty">
          No recent tasks match “{filter}”.</p>}
        {groups.map(group => <div className="task-group" key={group.label}>
          <span className="group-label">{group.label}</span>
          {group.items.map(task => <button key={task.task_id} type="button"
            className={`task ${selectedId === task.task_id ? "selected" : ""}`}
            aria-current={selectedId === task.task_id ? "true" : undefined}
            onClick={() => onSelect(task.task_id)}>
            <span className="task-icon"><Icon name="chat" size={15} /></span>
            <span className="task-main"><span className="title">
              {(task.objective || "(no objective)").slice(0, 120)}</span>
              <span className="meta"><span className={`status-dot ${statusOf(task)}`} />
                {task.run?.running ? "Running" : task.verification || task.status || "No verdict"}</span>
            </span>
          </button>)}
        </div>)}
      </div>
      {tasks.length >= 60 && <p className="sidebar-footnote">Showing the latest 60 tasks.</p>}
    </div>

    <nav className="sidebar-foot" aria-label="Studio navigation">
      <button className="foot-item" type="button" onClick={() => onOpenSheet("capabilities")}>
        <Icon name="layers" size={18} /> Capabilities</button>
      <button className="foot-item" type="button" onClick={() => onOpenSheet("journal")}>
        <Icon name="activity" size={18} /> Activity &amp; evidence</button>
      <button className="foot-item" type="button" onClick={() => onOpenSheet("settings")}>
        <Icon name="settings" size={18} /> Settings</button>
      <button className="foot-item" type="button" onClick={onTheme}>
        <Icon name={theme === "dark" ? "sun" : "moon"} size={18} /> Theme
        <span className="foot-note">{theme === "dark" ? "light" : "dark"}</span></button>
      <button className={`connection-card ${running && reachable ? "online" : "offline"}`}
        type="button" onClick={() => onOpenSheet("settings")}
        title={state?.provider?.detail || "Open provider settings"}>
        <span className={`connection-indicator ${running && reachable ? "on" : "off"}`} />
        <span className="connection-copy"><strong>{health}</strong>
          <small>{isDouble ? "scripted test double · no real model"
            : state?.settings?.model || state?.settings?.provider || "Configure a model"}</small></span>
        <Icon name="arrowRight" size={16} />
      </button>
    </nav>
  </aside>;
}
