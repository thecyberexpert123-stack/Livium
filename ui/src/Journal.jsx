import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get } from "./api.js";
import { Failure, KV } from "./common.jsx";

export function Journal({ onOpenCapabilities }) {
  const [type, setType] = useState("");
  const events = useQuery({ queryKey: ["events", type], queryFn: () => get(
    `/api/events?limit=120${type ? `&type=${encodeURIComponent(type)}` : ""}`),
    refetchInterval: 8000 });
  const capabilities = useQuery({ queryKey: ["capabilities"], queryFn: () => get("/api/capabilities"),
    refetchInterval: 8000 });
  const discovery = useQuery({ queryKey: ["discovery"], queryFn: () => get("/api/discovery?limit=6"),
    refetchInterval: 8000 });
  const integrity = useQuery({ queryKey: ["integrity"], queryFn: () => get("/api/integrity"),
    refetchInterval: 8000 });
  const ledger = useQuery({ queryKey: ["ledger"], queryFn: () => get("/api/ledger?limit=8"),
    refetchInterval: 8000 });
  const rows = events.data?.events ?? [];
  const types = [...new Set(rows.map(entry => entry.type))].sort();
  const refresh = () => [events, capabilities, discovery, integrity, ledger]
    .forEach(query => query.refetch());
  return <div className="sheet-body">
    <div className="card wide"><div className="row">
      <select aria-label="Event type" value={type} onChange={e => setType(e.target.value)}>
        <option value="">all types</option>{types.map(name => <option key={name} value={name}>{name}</option>)}
      </select><button className="ghost" type="button" onClick={refresh}>reload</button>
      <span className="sub">{rows.length} event(s) · newest first</span></div>
      <Failure error={events.error} context="Events unavailable" />
      <div className="events">{rows.map(entry => <div className="event" key={entry.id}>
        <span className="type">{entry.type}</span> <span className="hint-inline">
          {entry.ts}{entry.task_id && ` · ${entry.task_id}`}</span>
        <div className="json">{JSON.stringify(entry.payload)?.slice(0, 400)}</div>
      </div>)}{!events.error && !rows.length && <p className="empty">No events.</p>}</div>
    </div>
    <div className="grid-3">
      <div className="card"><h4>Capabilities</h4>
        <Failure error={capabilities.error} context="Capabilities unavailable" />
        {(capabilities.data?.capabilities ?? []).map(capability =>
          <div className="cap-list-row" key={capability.name}><button className="link" type="button"
            onClick={onOpenCapabilities}>{capability.name}</button> · {capability.status}</div>)}
        {!capabilities.error && !capabilities.data?.capabilities?.length && <p className="empty">
          No capability has been proposed yet.</p>}
        <button className="ghost" type="button" onClick={onOpenCapabilities}>Review capabilities</button>
      </div>
      <div className="card"><h4>Deferred work</h4>
        <Failure error={discovery.error} context="Discovery queue unavailable" />
        {discovery.data && <><KV rows={[
          ["queue", Object.entries(discovery.data.counts ?? {}).map(([k, v]) => `${k} ${v}`).join(", ")],
          ["open items", (discovery.data.records ?? []).slice(0, 4).map(r => r.title).join(" · ")],
          ["cycles", (discovery.data.cycles ?? []).length],
          ["stopped", (discovery.data.stopped_cycles ?? []).length],
        ]} /><p className="hint">{discovery.data.note}</p></>}
      </div>
      <div className="card"><h4>Integrity</h4>
        <Failure error={integrity.error} context="Integrity record unavailable" />
        {integrity.data && <KV rows={[
          ["watched", integrity.data.components?.length],
          ["status", integrity.data.inspection?.status],
          ["detections", integrity.data.enforcement?.detections],
          ["recoveries", integrity.data.enforcement?.recoveries],
          ["unresolved", integrity.data.enforcement?.unresolved_detections?.length],
          ["frozen", integrity.data.frozen?.frozen ? "yes" : "no"],
        ]} />}</div>
    </div>
    <div className="card wide"><h4>Measured cost</h4>
      <Failure error={ledger.error} context="Ledger unavailable" />
      {(ledger.data?.rows ?? []).length ? <table><thead><tr>
        <th>task</th><th>status</th><th>verified</th><th>model calls</th>
        <th>actual tokens</th><th>estimate</th></tr></thead><tbody>
        {ledger.data.rows.map(row => <tr key={row.task_id}>
          <td className="mono">{String(row.task_id).slice(0, 8)}</td>
          <td>{row.status}</td><td>{row.verification_state ?? "—"}</td>
          <td className="mono">{row.model_calls}</td>
          <td className="mono">{row.prompt_tokens_actual == null && row.completion_tokens_actual == null
            ? "not reported" : `${row.prompt_tokens_actual ?? 0} + ${row.completion_tokens_actual ?? 0}`}</td>
          <td className="mono">{row.tokens_estimated ?? "—"}</td>
        </tr>)}</tbody></table> : !ledger.error && <p className="empty">No task has been measured yet.</p>}
    </div>
  </div>;
}
