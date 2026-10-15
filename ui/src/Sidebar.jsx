import React from "react";
import { Failure, healthBadges } from "./common.jsx";

export function Sidebar({ tasks, selectedId, state, error, onSelect, onNew,
                          onRefresh, onOpenSheet, theme, onTheme, onHide }) {
  return <aside className="sidebar" aria-label="Tasks and navigation">
    <div className="sidebar-top">
      <button className="icon-btn" type="button" title="Hide the sidebar"
        aria-label="Hide the sidebar" onClick={onHide}>☰</button>
      <button className="new-task" type="button" onClick={onNew}>
        <span className="plus">＋</span> New task
      </button>
    </div>
    <div className="sidebar-scroll">
      <div className="sidebar-label"><span>Tasks</span>
        <button className="link" type="button" onClick={onRefresh}>reload</button></div>
      <Failure error={error} context="Task history unavailable" />
      <div className="task-list" aria-label="Task history">
        {!error && !tasks.length && <p className="empty">No task has run in this home yet.</p>}
        {tasks.map(task => <button key={task.task_id} type="button"
          className={`task ${selectedId === task.task_id ? "selected" : ""}`}
          aria-current={selectedId === task.task_id ? "true" : undefined}
          onClick={() => onSelect(task.task_id)}>
          <span className="title">{(task.objective || "(no objective)").slice(0, 80)}</span>
          <span className="meta">{task.run?.running && <span className="dot">● running · </span>}
            {task.status || "unknown"}{task.verification && ` · ${task.verification}`}</span>
        </button>)}
      </div>
    </div>
    <div className="sidebar-foot">
      <button className="foot-item" type="button" onClick={() => onOpenSheet("capabilities")}>
        <span>◆</span> Capabilities <span className="foot-note">build &amp; review</span></button>
      <button className="foot-item" type="button" onClick={() => onOpenSheet("journal")}>
        <span>▤</span> Journal <span className="foot-note">events, integrity, cost</span></button>
      <button className="foot-item" type="button" onClick={() => onOpenSheet("settings")}>
        <span>⚙</span> Settings <span className="foot-note">provider, API key</span></button>
      <button className="foot-item" type="button" onClick={onTheme}>
        <span>{theme === "dark" ? "◑" : "◐"}</span> Theme
        <span className="foot-note">{theme}</span></button>
      <div className="health" aria-label="Runtime health">{healthBadges(state)}</div>
    </div>
  </aside>;
}
