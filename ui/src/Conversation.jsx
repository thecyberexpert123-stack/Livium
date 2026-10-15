import React, { useEffect, useRef, useState } from "react";
import { Badge, Failure, formatBytes, formatWhen, KV } from "./common.jsx";

const SUGGESTIONS = [
  ["Summarise the workspace", "Read every .txt file in the workspace and write report.md summarising them."],
  ["Inspect before you trust", "List the workspace, read what you find, and tell me which files are stale."],
  ["Build a missing capability", "Check for an existing tool first. If a reusable ability is missing, propose a capability, implement it and write meaningful tests in the capability lab, then validate it. Stop at Validated for my human review; do not claim it is active."],
];

function Meta({ meta }) {
  const parts = Object.entries(meta ?? {}).filter(([, value]) => value !== undefined && value !== null && value !== false && value !== "");
  return parts.length > 0 && <div className="meta">{parts.map(([key, value]) =>
    <span key={key}>{key}={Array.isArray(value) ? value.join(", ")
      : typeof value === "object" ? JSON.stringify(value) : String(value)}{"  "}</span>)}</div>;
}

export function Message({ message, onOpenFile }) {
  const { role, meta = {} } = message;
  if (role === "user") return <div className="msg user"><div className="bubble">{message.body}</div></div>;
  if (role === "assistant") return <div className="msg assistant">
    <div className="avatar">L</div><div className="col">
      <div className="name">{message.title || "model"} <span className="when">{formatWhen(message.at)}</span></div>
      <div className={`prose ${message.body ? "" : "none"}`}>
        {message.body || "(no text — the model asked for a tool)"}</div><Meta meta={meta} />
    </div></div>;
  if (role === "tool") {
    const tone = meta.denied ? "denied" : meta.ok ? "ok" : "failed";
    return <div className="msg tool"><div className="col"><details className={`chip ${tone}`}>
      <summary>{message.title} · {message.body}</summary><Meta meta={meta} />
    </details></div></div>;
  }
  if (role === "artifact") return <div className="msg artifact"><div className="col">
    <div className="file-card"><span className="icon">📄</span><span className="col">
      <span className="title">{message.title}</span><span className="sub">{message.body}</span>
    </span>{meta.root && meta.relative_path && <button className="ghost" type="button"
      onClick={() => onOpenFile(meta.root, meta.relative_path)}>Open</button>}</div>
  </div></div>;
  if (role === "refusal") return <div className="msg refusal"><div className="note">
    <strong>{message.title}</strong> {message.body}<Meta meta={meta} /></div></div>;
  // The task summary already carries the test-double disclaimer. Keep other
  // journalled system events; hiding them would lose real information.
  if (/^NOTE: this run used the scripted test double/i.test(message.body ?? "")) return null;
  return <div className="msg system"><div className="divider">
    {message.title || "event"}{message.body && ` — ${message.body}`}</div></div>;
}

export function Verification({ detail }) {
  const state = detail?.verification_state;
  if (!detail) return null;
  const criteria = detail.criteria ?? [];
  const tone = state?.state === "verified" ? "ok" : state?.state === "not verified" ? "bad"
    : state?.state === "not run" ? "" : "warn";
  return <div className="checks" aria-label="Acceptance checks">
    {state && <div className="line"><Badge tone={tone}>{detail.verification_label ?? state.state}</Badge>
      <span className="why">{state.source ?? ""}</span></div>}
    {!criteria.length ? <div className="line"><span className="why">
      This task has no acceptance criteria, so its outcome is unverified — the summary
      is the agent's own account, not an independent check.</span></div>
      : criteria.map((item, index) => <div className="line" key={index}>
        <Badge tone={item.outcome === "passed" ? "ok" : (item.outcome === "failed" || item.outcome === "error") ? "bad" : "warn"}>
          {item.outcome ?? "no outcome recorded"}</Badge>
        <span className="spec">{JSON.stringify(item.spec)}</span>
        {item.detail && <span className="why">{item.detail}</span>}
      </div>)}
  </div>;
}

export function RunDetails({ run, detail, onOpenFile }) {
  const ref = useRef(null);
  useEffect(() => { if (run?.running && ref.current) ref.current.open = true; }, [run?.running]);
  if (!run) return null;
  const artifacts = detail?.artifacts ?? [];
  return <details ref={ref} className="run-details" data-testid="run-details">
    <summary>Run · {run.status} {run.exit_code == null ? "" : `(exit ${run.exit_code})`}
      {run.running ? " · live" : " · finished"}</summary>
    <div className="run-grid">
      <div><h4>Process</h4><KV rows={[
        ["task id", run.task_id], ["run id", run.id], ["process", run.pid],
        ["status", run.status], ["started", formatWhen(run.started_at)],
        ["finished", formatWhen(run.finished_at)],
      ]} /></div>
      <div><h4>Files this task wrote</h4>
        {!artifacts.length && <p className="empty">Nothing written by this task.</p>}
        {artifacts.map((item, i) => <div className="artifact-row" key={`${item.path}-${i}`}>
          <span className="mono">{item.path}</span> · {formatBytes(item.bytes)}
          {item.sha256 && <span className="mono"> · sha256 {item.sha256.slice(0, 12)}…</span>}
          {item.root && item.relative_path && <button className="link" type="button"
            onClick={() => onOpenFile(item.root, item.relative_path)}>open</button>}
        </div>)}
      </div>
      <div className="wide"><h4>Steps</h4>
        {!detail?.steps?.length ? <p className="empty">No steps recorded for this task.</p>
          : <table><thead><tr><th>#</th><th>phase</th><th>status</th><th>detail</th></tr></thead>
            <tbody>{detail.steps.map((step, i) => <tr key={step.id ?? i}>
              <td className="mono">{step.idx}</td><td>{step.phase}</td><td>{step.status}</td>
              <td>{String(step.detail ?? "").slice(0, 220)}</td></tr>)}</tbody></table>}
      </div>
      <div className="wide"><h4>Process output <span className="hint-inline">stdout and stderr, as it arrives</span></h4>
        <pre className="log">{run.log_tail || "(no output yet)"}</pre></div>
    </div>
  </details>;
}

export function Composer({ resetToken, suggestion, selectedId, busy, onSubmit, onError }) {
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [force, setForce] = useState(false);
  const [resume, setResume] = useState(false);
  const [options, setOptions] = useState(false);
  const input = useRef(null);
  useEffect(() => { setObjective(""); setResume(false); input.current?.focus(); }, [resetToken]);
  useEffect(() => { if (suggestion) {
    setObjective(suggestion.text);
    input.current?.focus();
  } }, [suggestion]);
  useEffect(() => { if (input.current) {
    input.current.style.height = "auto";
    input.current.style.height = `${Math.min(input.current.scrollHeight, 200)}px`;
  } }, [objective]);

  function submit(event) {
    event.preventDefault();
    if (!objective.trim()) { onError("Write what you want the agent to do first."); return; }
    let checks = null;
    if (criteria.trim()) {
      try { checks = JSON.parse(criteria); }
      catch (error) { onError(`Acceptance criteria are not JSON: ${error.message}`); return; }
      if (!Array.isArray(checks)) { onError("Acceptance criteria must be a JSON array."); return; }
    }
    Promise.resolve(onSubmit({ objective: objective.trim(), criteria: checks, force,
               ...(resume && selectedId ? { task_id: selectedId, resume: true } : {}) }))
      .then(() => setObjective(""))
      .catch(error => onError(`The task could not be started: ${error.message}`));
  }

  return <form className="composer" onSubmit={submit} aria-label="New task">
    <div className="composer-box">
      <textarea ref={input} aria-label="Task objective" rows={1} value={objective}
        onChange={e => setObjective(e.target.value)}
        onKeyDown={e => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault(); e.currentTarget.form.requestSubmit();
          }
        }} placeholder="Ask the agent to do something in the workspace… (Enter to run, Shift+Enter for a new line)" />
      <button type="submit" disabled={busy} title="Start the task" aria-label="Start the task">➤</button>
    </div>
    <div className="composer-tools">
      <button className="chip-btn" type="button" aria-expanded={options} onClick={() => setOptions(!options)}>
        Acceptance criteria &amp; options</button>
      <span className="hint">Runs as a real process · transcript from the runtime journal</span>
    </div>
    {options && <div className="composer-options">
      <label className="field"><span>Acceptance criteria <em>optional, JSON</em></span>
        <input value={criteria} onChange={e => setCriteria(e.target.value)}
          placeholder='[{"kind":"file_exists","path":"report.md"}]' /></label>
      <label className="check"><input type="checkbox" checked={force}
        onChange={e => setForce(e.target.checked)} /> run even if the provider probe fails</label>
      <label className="check"><input type="checkbox" checked={resume} disabled={!selectedId}
        onChange={e => setResume(e.target.checked)} /> resume the selected task id</label>
    </div>}
  </form>;
}

export function Conversation({ selectedId, detail, error, run, banner, busy, resetToken,
                               onOpenFile, onStop, onSubmit, onError, onShowSidebar, onToggleDock,
                               dockOpen }) {
  const scroll = useRef(null);
  const [suggestion, setSuggestion] = useState(null);
  const messageCount = detail?.messages?.length ?? 0;
  useEffect(() => {
    if (!scroll.current) return;
    const nearBottom = scroll.current.scrollHeight - scroll.current.scrollTop - scroll.current.clientHeight < 160;
    if (nearBottom) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [selectedId, messageCount]);
  return <main className="chat">
    <header className="chat-head">
      <button className="icon-btn" type="button" title="Show the sidebar" aria-label="Show the sidebar"
        onClick={onShowSidebar}>☰</button>
      <div className="chat-title">
        <strong>{detail?.task?.objective?.slice(0, 160) || (selectedId || "New task")}</strong>
        <span className="sub">{detail
          ? `${detail.counts.messages} message(s) · ${detail.counts.artifacts} file(s) written · ${detail.counts.refusals} refusal(s) · ${selectedId}`
          : "The agent works in the workspace on the right."}</span>
      </div>
      <div className="chat-actions">
        {run && <Badge tone={run.running ? "accent" : run.exit_code === 0 ? "ok" : "bad"}>
          run {run.status}</Badge>}
        {run?.running && <button className="ghost danger" type="button" onClick={onStop}>stop</button>}
        <button className={`icon-btn ${dockOpen ? "active" : ""}`} type="button"
          aria-label="Show or hide the workspace" aria-pressed={dockOpen} onClick={onToggleDock}>▥</button>
      </div>
    </header>
    {banner && <div className="banner" role="alert">{banner}</div>}
    <div ref={scroll} className="scroll">
      {!selectedId && <div className="welcome"><h2>What should the agent do?</h2>
        <p className="dim">It runs as a real process in a real directory. Its steps,
          refusals and files come back from the runtime journal, not a generated story.
          The model decides the work; the runtime decides permissions and checks.</p>
        <div className="suggestions">{SUGGESTIONS.map(([title, text]) => <button
          className="suggestion" type="button" key={title}
          onClick={() => setSuggestion({ text })}>
          <strong>{title}</strong><span>{text}</span></button>)}</div></div>}
      {selectedId && <>
        <Failure error={error} context="Cannot read the journal for this task" />
        {!detail && !error && <p className="empty">Waiting for the first journal entry…</p>}
        <RunDetails run={run} detail={detail} onOpenFile={onOpenFile} />
        <Verification detail={detail} />
        <div className="transcript" aria-label="Task transcript">
          {detail && !detail.messages?.length && <p className="empty">No messages were journalled yet.</p>}
          {(detail?.messages ?? []).map((message, i) => <Message key={`${message.at ?? ""}-${i}`}
            message={message} onOpenFile={onOpenFile} />)}
        </div>
      </>}
    </div>
    <Composer resetToken={resetToken} suggestion={suggestion} selectedId={selectedId} busy={busy}
      onSubmit={onSubmit} onError={onError} />
  </main>;
}
