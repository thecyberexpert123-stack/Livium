import React, { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Badge, Failure, formatBytes, formatWhen, KV } from "./common.jsx";
import { Icon } from "./icons.jsx";

// These are prompts, not canned replies. Clicking one only fills the real task
// composer; sending still goes through Node -> Python -> the configured provider.
const SUGGESTIONS = [
  { icon: "file", title: "Summarise the workspace", caption: "Turn your files into a useful brief",
    text: "Read every .txt file in the workspace and write report.md summarising them." },
  { icon: "search", title: "Inspect before you trust", caption: "Find what changed and why",
    text: "List the workspace, read what you find, and tell me which files are stale." },
  { icon: "wand", title: "Build a missing capability", caption: "Propose, code and test a new tool",
    text: "Check for an existing tool first. If a reusable ability is missing, propose a capability, implement it and write meaningful tests in the capability lab, then validate it. Stop at Validated for my human review; do not claim it is active." },
];
const MARKDOWN_PLUGINS = [remarkGfm];

function asText(node) {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(asText).join("");
  if (React.isValidElement(node)) return asText(node.props.children);
  return "";
}

function CopyButton({ text, label = "Copy response" }) {
  const [result, setResult] = useState("");
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard access is unavailable");
      await navigator.clipboard.writeText(text);
      setResult("Copied");
    } catch { setResult("Copy unavailable"); }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setResult(""), 2600);
  }
  return <button className={`copy-button ${result === "Copy unavailable" ? "copy-error" : ""}`}
    type="button" aria-label={result || label} title={result || label} onClick={copy}>
    <Icon name={result === "Copied" ? "check" : "copy"} size={15} />
    <span>{result || "Copy"}</span>
  </button>;
}

function Markdown({ text }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={MARKDOWN_PLUGINS}
    components={{
      a: ({ node, href, ...props }) => href
        ? <a href={href} {...props} target="_blank" rel="noopener noreferrer" />
        : <span {...props} />, // react-markdown's default URL filter removed an unsafe href
      img: ({ alt }) => <span className="blocked-image">[External image omitted{alt ? `: ${alt}` : ""}]</span>,
      pre: ({ children }) => <div className="code-frame">
        <div className="code-head"><span>Code</span><CopyButton text={asText(children)} label="Copy code" /></div>
        <pre>{children}</pre></div>,
    }}>{text}</ReactMarkdown></div>;
}

function Meta({ meta }) {
  const parts = Object.entries(meta ?? {}).filter(([, value]) =>
    value !== undefined && value !== null && value !== false && value !== "");
  if (!parts.length) return null;
  return <details className="message-meta"><summary>Recorded details</summary><div className="meta">
    {parts.map(([key, value]) => <span key={key}>{key}={Array.isArray(value) ? value.join(", ")
      : typeof value === "object" ? JSON.stringify(value) : String(value)}{"  "}</span>)}
  </div></details>;
}

export function Message({ message, onOpenFile, testDouble = false }) {
  const { role, meta = {} } = message;
  if (role === "user") return <div className="msg user"><div className="user-message">
    <span className="speaker">You <span className="when">{formatWhen(message.at)}</span></span>
    <div className="bubble">{message.body}</div>
    {meta.journalled_objective && <p className="resume-note">This resumed run was started with:
      <span>{meta.journalled_objective}</span></p>}
  </div></div>;

  if (role === "assistant") return <div className="msg assistant">
    <span className="avatar"><Icon name="sparkles" size={19} /></span>
    <div className="col"><div className="message-heading"><strong>Livium</strong>
      {meta.test_double && <Badge tone="warn">test double</Badge>}
      <span className="when">{formatWhen(message.at)}</span></div>
      {message.body ? <Markdown text={message.body} />
        : <p className="empty assistant-empty">The model requested a tool without a text reply.
            Its actions are in the activity below.</p>}
      <div className="message-actions">{message.body && <CopyButton text={message.body} />}
        <Meta meta={meta} /></div>
    </div>
  </div>;

  if (role === "tool") {
    const tone = meta.denied ? "denied" : meta.ok ? "ok" : "failed";
    return <div className="msg tool"><details className={`chip ${tone}`}>
      <summary><Icon name={tone === "ok" ? "check" : "terminal"} size={15} />
        <span><strong>{message.title}</strong> · {message.body}</span></summary>
      <Meta meta={meta} />
    </details></div>;
  }

  if (role === "artifact") return <div className="msg artifact">
    <div className="file-card"><span className="artifact-icon"><Icon name="file" size={20} /></span>
      <span className="col"><span className="title">{message.title}</span>
        <span className="sub">{message.body}</span></span>
      {meta.root && meta.relative_path && <button className="ghost" type="button"
        onClick={() => onOpenFile(meta.root, meta.relative_path)}>Open</button>}
    </div>
  </div>;

  if (role === "refusal") return <div className="msg refusal"><div className="note">
    <Icon name="alert" size={17} /><div><strong>{message.title}</strong>
      <p>{message.body}</p><Meta meta={meta} /></div></div></div>;

  // A finish_task summary is the agent's own claim, not a fact certified by the
  // harness. Present it as the chat answer, but keep verification a separate card.
  if (message.title === "task finished") return <div className="msg assistant summary-message">
    <span className="avatar"><Icon name="sparkles" size={19} /></span>
    <div className="col"><div className="message-heading"><strong>Run summary</strong>
      <Badge tone="accent">agent claim</Badge>
      {testDouble && <Badge tone="warn">test double</Badge>}</div>
      {message.body ? <Markdown text={message.body} />
        : <p className="empty">The agent did not record a final summary.</p>}
      <div className="message-actions">{message.body && <CopyButton text={message.body} />}
        <span className="sub">Check the runtime verdict below.</span></div>
      <Meta meta={meta} />
    </div>
  </div>;

  if (/^NOTE: this run used the scripted test double/i.test(message.body ?? "")) return null;
  return <div className="msg system"><div className="divider">
    <span>{message.title || "event"}{message.body && ` · ${message.body}`}</span>
  </div></div>;
}

function activityMessage(message) {
  return message.role === "tool" || (message.role === "system" && message.title !== "task finished");
}

function transcriptGroups(messages) {
  const groups = [];
  for (const [index, message] of messages.entries()) {
    if (message.role === "system" && /^NOTE: this run used the scripted test double/i.test(message.body ?? "")) continue;
    if (activityMessage(message)) {
      const last = groups.at(-1);
      if (last?.kind === "activity") last.items.push({ message, index });
      else groups.push({ kind: "activity", items: [{ message, index }] });
    } else groups.push({ kind: "message", items: [{ message, index }] });
  }
  return groups;
}

function ActivityGroup({ items, onOpenFile }) {
  const calls = items.filter(({ message }) => message.role === "tool").length;
  const failed = items.filter(({ message }) => message.role === "tool" &&
    (message.meta?.denied || message.meta?.ok === false)).length;
  return <details className={`activity-group ${failed ? "has-failures" : ""}`}>
    <summary><span className="activity-icon"><Icon name="activity" size={16} /></span>
      <span>{calls ? `${calls} recorded action${calls === 1 ? "" : "s"}` : "Runtime activity"}</span>
      {failed > 0 && <span className="activity-failure">{failed} refused / failed</span>}
      <span className="activity-count">{items.length} event{items.length === 1 ? "" : "s"}</span>
      <Icon name="chevronDown" size={16} className="activity-chevron" />
    </summary>
    <div className="activity-contents">{items.map(({ message, index }) =>
      <Message key={index} message={message} onOpenFile={onOpenFile} />)}</div>
  </details>;
}

export function Verification({ detail }) {
  const state = detail?.verification_state;
  if (!detail) return null;
  const criteria = detail.criteria ?? [];
  const tone = state?.state === "verified" ? "ok" : state?.state === "not verified" ? "bad"
    : state?.state === "not run" ? "" : "warn";
  return <section className={`checks ${tone}`} aria-label="Acceptance checks">
    <span className="checks-icon"><Icon name={tone === "ok" ? "check" : "shield"} size={18} /></span>
    <div className="checks-content"><div className="checks-title">
      <strong>Runtime verification</strong><Badge tone={tone}>{detail.verification_label ?? state?.state ?? "not reported"}</Badge>
    </div>
      {!criteria.length ? <p className="hint">No acceptance criteria were supplied. This task is
        unverified; its summary is the agent's account, not an independent check.</p>
        : <details className="checks-detail" open={tone === "bad" ? true : undefined}>
          <summary>See {criteria.length} acceptance check{criteria.length === 1 ? "" : "s"}</summary>
          {criteria.map((item, index) => <div className="check-line" key={index}>
            <Badge tone={item.outcome === "passed" ? "ok" : (item.outcome === "failed" || item.outcome === "error") ? "bad" : "warn"}>
              {item.outcome ?? "no outcome recorded"}</Badge>
            <code>{JSON.stringify(item.spec)}</code>
            {item.detail && <span className="hint">{item.detail}</span>}
          </div>)}
        </details>}
      {state?.source && <span className="checks-source">{state.source}</span>}
    </div>
  </section>;
}

export function RunDetails({ run, detail, onOpenFile }) {
  const ref = useRef(null);
  useEffect(() => { if (run?.running && ref.current) ref.current.open = true; }, [run?.running]);
  if (!run) return null;
  const artifacts = detail?.artifacts ?? [];
  return <details ref={ref} className="run-details" data-testid="run-details">
    <summary><Icon name="terminal" size={16} /> Technical run details · {run.status}
      {run.exit_code == null ? "" : ` (exit ${run.exit_code})`}</summary>
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
      <div className="wide"><h4>Process output <span className="hint-inline">stdout and stderr as recorded</span></h4>
        <pre className="log">{run.log_tail || "(no output yet)"}</pre></div>
    </div>
  </details>;
}

export function Composer({ resetToken, suggestion, selectedId, busy, onSubmit, onError, onAttach }) {
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [force, setForce] = useState(false);
  const [resume, setResume] = useState(false);
  const [options, setOptions] = useState(false);
  const [attachments, setAttachments] = useState([]);
  const [uploading, setUploading] = useState(false);
  const input = useRef(null);
  const fileInput = useRef(null);
  useEffect(() => {
    setObjective(""); setCriteria(""); setForce(false); setResume(false);
    setOptions(false); setAttachments([]); input.current?.focus();
  }, [resetToken]);
  useEffect(() => { if (suggestion) {
    setObjective(suggestion.text);
    input.current?.focus();
  } }, [suggestion]);
  useEffect(() => { setResume(false); }, [selectedId]);
  useEffect(() => { if (input.current) {
    input.current.style.height = "auto";
    input.current.style.height = `${Math.min(input.current.scrollHeight, 180)}px`;
  } }, [objective]);

  async function addFiles(files) {
    if (!files?.length || !onAttach || uploading) return;
    setUploading(true);
    try {
      const placed = await onAttach(files);
      setAttachments(previous => [...previous, ...placed.filter(item =>
        !previous.some(existing => existing.path === item.path))]);
    } catch (error) { onError(`Attachment upload failed: ${error.message}`); }
    finally { setUploading(false); }
  }

  function submit(event) {
    event.preventDefault();
    if (busy || uploading) return;
    const typed = objective.trim();
    if (!typed && !attachments.length) { onError("Write a message or attach a file first."); return; }
    let checks = null;
    if (criteria.trim()) {
      try { checks = JSON.parse(criteria); }
      catch (error) { onError(`Acceptance criteria are not JSON: ${error.message}`); return; }
      if (!Array.isArray(checks)) { onError("Acceptance criteria must be a JSON array."); return; }
    }
    const prompt = typed || "Inspect the attached workspace files and summarise what they contain.";
    const objectiveWithFiles = attachments.length
      ? `${prompt}\n\nAttached workspace files: ${attachments.map(item => JSON.stringify(item.path)).join(", ")}. Read them when relevant.`
      : prompt;
    Promise.resolve(onSubmit({ objective: objectiveWithFiles, criteria: checks, force,
               ...(resume && selectedId ? { task_id: selectedId, resume: true } : {}) }))
      .then(() => { setObjective(""); setAttachments([]); setCriteria("");
        setForce(false); setResume(false); setOptions(false); })
      .catch(error => onError(`The task could not be started: ${error.message}`));
  }

  return <form className="composer" onSubmit={submit} aria-label="New task"
    onDragOver={event => { if (event.dataTransfer?.types?.includes("Files")) event.preventDefault(); }}
    onDrop={event => { if (!event.dataTransfer?.files?.length) return;
      event.preventDefault(); addFiles([...event.dataTransfer.files]); }}>
    <div className="composer-inner">
      {attachments.length > 0 && <div className="attached-files" aria-label="Attached workspace files">
        {attachments.map(item => <span className="attachment" key={item.path}>
          <Icon name="file" size={14} /><span>{item.path}</span>
          <button className="icon-btn" type="button" aria-label={`Remove ${item.path} from this task`}
            title="Remove from this prompt (the workspace file remains)"
            onClick={() => setAttachments(previous => previous.filter(file => file.path !== item.path))}>
            <Icon name="x" size={13} /></button>
        </span>)}
      </div>}
      <div className="composer-box">
        <textarea ref={input} aria-label="Task objective" rows={1} value={objective}
          onChange={event => setObjective(event.target.value)}
          onKeyDown={event => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault(); event.currentTarget.form.requestSubmit();
            }
          }} placeholder="Message Livium…" />
        <div className="composer-controls">
          <button className="icon-btn attach-btn" type="button" disabled={uploading || busy}
            title="Upload a file to the workspace" aria-label="Attach files to workspace"
            onClick={() => fileInput.current?.click()}><Icon name="paperclip" size={19} /></button>
          <input ref={fileInput} type="file" multiple hidden aria-label="Choose files to attach"
            onChange={event => { addFiles([...event.target.files]); event.target.value = ""; }} />
          <span className="composer-key">↵</span>
          <button className="send-btn" type="submit" disabled={busy || uploading || (!objective.trim() && !attachments.length)}
            title="Start the task" aria-label="Start the task"><Icon name="arrowUp" size={20} /></button>
        </div>
      </div>
      <div className="composer-tools">
        <button className="chip-btn" type="button" aria-expanded={options} onClick={() => setOptions(!options)}>
          <Icon name="settings" size={14} /> Acceptance criteria &amp; options
          <Icon name="chevronDown" size={13} /></button>
        <span className="composer-footnote">{uploading ? "Uploading to workspace…" :
          "New task per send (unless resuming) · no criteria means unverified"}</span>
      </div>
      {options && <div className="composer-options">
        <label className="field"><span>Acceptance criteria <em>optional JSON array</em></span>
          <input value={criteria} onChange={event => setCriteria(event.target.value)}
            placeholder='[{"kind":"file_exists","path":"report.md"}]' /></label>
        <label className="check"><input type="checkbox" checked={force}
          onChange={event => setForce(event.target.checked)} /> Run even if the provider probe fails</label>
        <label className="check"><input type="checkbox" checked={resume} disabled={!selectedId}
          onChange={event => setResume(event.target.checked)} /> Resume an interrupted selected task
          (not a chat follow-up)</label>
      </div>}
    </div>
  </form>;
}

function ConnectionNotice({ state, onOpenSettings }) {
  const provider = state?.provider;
  if (!state || (state.backend?.running && provider?.ok === true && !provider?.is_test_double)) return null;
  const isDouble = provider?.is_test_double === true || state.settings?.provider === "scripted";
  const offline = !state.backend?.running || provider?.ok === false;
  const checking = state.backend?.running && provider?.ok == null;
  return <div className={`connection-notice ${offline ? "offline" : checking ? "pending" : "demo"}`}>
    <span className="notice-icon"><Icon name={offline ? "alert" : "shield"} size={19} /></span>
    <div><strong>{!state.backend?.running ? "Runtime is offline" : provider?.ok === false
      ? "Model connection needs attention" : checking ? "Checking the model connection"
      : "Demo mode, not live AI"}</strong>
      <p>{!state.backend?.running ? state.backend?.last_error || "Restart the runtime from Settings."
        : checking ? "No verified provider response has been recorded yet."
        : isDouble && !offline ? "This home uses the scripted test double. Its replies are not model inference."
        : provider?.detail || "No verified model connection is available."}</p></div>
    <button className="ghost" type="button" onClick={onOpenSettings}>Open settings
      <Icon name="arrowRight" size={15} /></button>
  </div>;
}

export function Conversation({ selectedId, detail, error, state, run, banner, busy, resetToken,
                               onOpenFile, onStop, onSubmit, onAttach, onError, onNew,
                               onOpenSettings, onShowSidebar, onToggleDock, dockOpen }) {
  const scroll = useRef(null);
  const [suggestion, setSuggestion] = useState(null);
  const messageCount = detail?.messages?.length ?? 0;
  const testDouble = (detail?.messages ?? []).some(message =>
    message.role === "assistant" && message.meta?.test_double === true);
  useEffect(() => {
    if (!scroll.current) return;
    const nearBottom = scroll.current.scrollHeight - scroll.current.scrollTop - scroll.current.clientHeight < 160;
    if (nearBottom) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [selectedId, messageCount]);
  const provider = state?.provider;
  const isDouble = provider?.is_test_double === true || state?.settings?.provider === "scripted";
  const providerLabel = isDouble ? "Scripted test double" : state?.settings?.model ||
    state?.settings?.provider || "Configure model";
  const providerReady = state?.backend?.running && provider?.ok === true;
  const groups = transcriptGroups(detail?.messages ?? []);

  return <main className="chat">
    <header className="chat-head">
      <button className="icon-btn show-sidebar" type="button" title="Show conversations"
        aria-label="Show the sidebar" onClick={onShowSidebar}><Icon name="sidebar" size={19} /></button>
      <div className="chat-title"><span className="eyebrow">LIVIUM / AGENT CHAT</span>
        <strong>{detail?.task?.objective?.slice(0, 90) || (selectedId ? "Task in progress" : "New conversation")}</strong>
      </div>
      <div className="chat-actions">
        <button className={`model-pill ${providerReady ? "ready" : "unavailable"}`} type="button"
          aria-label={`Provider settings: ${providerLabel}`}
          title={provider?.detail || "Open provider settings"} onClick={onOpenSettings}>
          <span className="model-dot" /><span>{providerLabel}</span><Icon name="chevronDown" size={14} />
        </button>
        {run?.running && <button className="icon-btn stop-btn" type="button"
          title="Stop the running task" aria-label="Stop run" onClick={onStop}>
          <Icon name="stop" size={18} /></button>}
        <button className="icon-btn desktop-new" type="button" title="Start a new chat"
          aria-label="Start new chat" onClick={onNew}><Icon name="plus" size={19} /></button>
        <button className={`icon-btn ${dockOpen ? "active" : ""}`} type="button"
          title="Show or hide the workspace" aria-label="Show or hide the workspace"
          aria-pressed={dockOpen} onClick={onToggleDock}><Icon name="panel" size={19} /></button>
      </div>
    </header>

    {banner && <div className="banner" role="alert"><Icon name="alert" size={18} />
      <span>{banner}</span><button className="icon-btn" type="button" aria-label="Dismiss message"
        onClick={() => onError("")}><Icon name="x" size={15} /></button></div>}

    <div ref={scroll} className="scroll">
      {!selectedId && <div className="welcome">
        <span className="welcome-mark"><Icon name="sparkles" size={31} /></span>
        <span className="welcome-eyebrow">A THINKING SPACE FOR REAL WORK</span>
        <h1>What can I help you<br /><em>work through?</em></h1>
        <p className="welcome-description">Ask a question, make sense of a file, or give your
          agent a project. Your workspace, tools, and evidence stay close at hand.</p>
        <ConnectionNotice state={state} onOpenSettings={onOpenSettings} />
        <div className="suggestions" aria-label="Suggested tasks">
          {SUGGESTIONS.map(item => <button className="suggestion" type="button" key={item.title}
            onClick={() => setSuggestion({ text: item.text })}>
            <span className="suggestion-icon"><Icon name={item.icon} size={19} /></span>
            <span><strong>{item.title}</strong><small>{item.caption}</small></span>
            <Icon name="arrowRight" size={16} className="suggestion-arrow" />
          </button>)}
        </div>
      </div>}

      {selectedId && <div className="conversation-content">
        {!(!detail && run?.status === "failed") && <Failure error={error}
          context="Cannot read the journal for this task" />}
        {!detail && run?.status === "failed" && <div className="run-failed" role="alert">
          <Icon name="alert" size={22} /><div><strong>The task could not start</strong>
            <p>The runtime did not record a completed conversation. Its process reported:</p>
            <pre className="log">{run.log_tail || error?.message || "No diagnostic output was recorded."}</pre>
            <button type="button" className="ghost" onClick={onOpenSettings}>Check provider settings</button>
          </div></div>}
        {!detail && run?.status !== "failed" && <>
          {run?.objective && <Message message={{ role: "user", body: run.objective }} onOpenFile={onOpenFile} />}
          <p className="journal-wait"><span className="pulse-dot" /> Waiting for the first runtime journal entry…</p>
        </>}
        {detail && <div className="transcript" aria-label="Task transcript">
          {!detail.messages?.length && <p className="empty">No messages were journalled yet.</p>}
          {groups.map((group, index) => group.kind === "activity"
            ? <ActivityGroup key={`activity-${index}`} items={group.items} onOpenFile={onOpenFile} />
            : <Message key={`message-${group.items[0].index}`} message={group.items[0].message}
                onOpenFile={onOpenFile} testDouble={testDouble} />)}
        </div>}
        {run?.running && <div className="working" role="status">
          <span className="working-mark"><Icon name="sparkles" size={17} /></span>
          <div><strong>Livium is working</strong><span>Only recorded steps appear above.
            {detail?.steps?.length ? ` Latest: ${detail.steps.at(-1).phase}.` : " Waiting for the runtime."}</span></div>
          <span className="working-dots"><i /><i /><i /></span>
        </div>}
        <Verification detail={detail} />
        <RunDetails run={run} detail={detail} onOpenFile={onOpenFile} />
      </div>}
    </div>

    <Composer resetToken={resetToken} suggestion={suggestion} selectedId={selectedId} busy={busy}
      onSubmit={onSubmit} onAttach={onAttach} onError={onError} />
  </main>;
}
