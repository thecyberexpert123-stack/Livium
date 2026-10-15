import React, { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get } from "./api.js";
import { Badge, Failure, KV } from "./common.jsx";

const shellQuote = value => `'${String(value ?? "").replace(/'/g, "'\\''")}'`;

export function Capabilities({ state, onOpenFile }) {
  const [selected, setSelected] = useState(null);
  const listing = useQuery({ queryKey: ["capabilities"], queryFn: () => get("/api/capabilities"),
    refetchInterval: 8000 });
  const rows = listing.data?.capabilities ?? [];
  useEffect(() => {
    if (!selected && rows.length) setSelected(rows[0].name);
  }, [selected, rows]);
  const review = useQuery({ queryKey: ["capability-review", selected],
    queryFn: () => get(`/api/capabilities/${encodeURIComponent(selected)}/review`),
    enabled: Boolean(selected), refetchInterval: 8000 });
  const report = review.data;
  const candidate = report?.capability ?? rows.find(row => row.name === selected);
  const home = state?.studio?.home;
  const promoted = state?.studio?.roots?.promoted?.path;
  const python = state?.studio?.python || "python3";
  const env = home && promoted ? `LIVIUM_HOME=${shellQuote(home)} LIVIUM_PROMOTED_DIR=${shellQuote(promoted)} ` : "";
  const command = action => `${env}${shellQuote(python)} -m livium.cli capability ${action} ${shellQuote(selected)}`;
  return <div className="sheet-body">
    <div className="card wide"><h4>The lifecycle is executable, not a label</h4>
      <p className="hint">The agent can propose a need, create lab files and run their real tests.
        Passing code reaches <strong>Validated</strong>, not Active. The exact source,
        helpers, resources and tests are bound to a SHA-256 manifest. A named human reviews
        the evidence and the Guardian before promoting from the CLI. On the next task,
        the tool loads only if the installed files still match. A successful use moves
        Active to Maintained; a human can deprecate a superseded tool, or rollback
        a defect. Neither will load in the next run.</p>
      <p className="hint">This Studio has no login, so it deliberately offers no HTTP
        promotion button. A name supplied in a browser form would not prove human
        authorization. The CLI action must be performed by the operator with access
        to this runtime home. This is not a filesystem sandbox for authored Python.</p>
      <div className="row"><button className="ghost" type="button" onClick={() => {
        listing.refetch(); review.refetch(); }}>Reload evidence</button>
        {listing.data?.load_failures?.length > 0 && <Badge tone="bad">
          {listing.data.load_failures.length} load failure(s)</Badge>}
      </div>
      <Failure error={listing.error} context="Capability registry unavailable" />
      {(listing.data?.load_failures ?? []).map((failure, i) =>
        <p className="failure" key={i}>{failure}</p>)}
    </div>
    <div className="capability-grid">
      <div className="card capability-list"><h4>Recorded capabilities</h4>
        {!rows.length && !listing.error && <p className="empty">Nothing has been proposed yet.
          Ask the agent for a reusable missing ability, not just a new file.</p>}
        {rows.map(cap => <button className={`capability-item ${selected === cap.name ? "selected" : ""}`}
          type="button" key={cap.name} onClick={() => setSelected(cap.name)}>
          <strong>{cap.name}</strong><Badge tone={cap.status === "active" || cap.status === "maintained"
            ? "ok" : cap.status === "validated" ? "accent" : "warn"}>{cap.status}</Badge>
          <span className="sub">{cap.requirement}</span></button>)}
      </div>
      <div className="card capability-inspector"><h4>{selected || "Select a capability"}</h4>
        <Failure error={review.error} context="Review unavailable" />
        {candidate && <><KV rows={[
          ["status", candidate.status], ["requirement", candidate.requirement],
          ["reuse analysis", candidate.reuse_analysis],
          ["verification", candidate.verification || "not tested"],
          ["promoted by", candidate.promoted_by || "not promoted"],
          ["promoted at", candidate.promoted_at || "not promoted"],
          ["limitations", candidate.limitations],
        ]} />
          <h4>Tools actually declared</h4>
          {candidate.tools?.length ? <ul>{candidate.tools.map(tool =>
            <li key={tool.name}><code>{tool.name}</code> — {tool.description}</li>)}</ul>
            : <p className="empty">No tool interface is validated yet.</p>}
        </>}
        {report && <>
          <h4>Tested files and current lab</h4>
          <Badge tone={report.eligible_for_human_review ? "ok" : "warn"}>
            {report.eligible_for_human_review ? "exact tested files still present" : "not eligible for promotion"}</Badge>
          <KV rows={[
            ["tested manifest", report.lab.expected_sha256 || "no passing tests recorded"],
            ["current manifest", report.lab.observed_sha256 || "no lab files"],
            ["unchanged", report.lab.unchanged ? "yes" : "no"],
            ["guardian gate", report.guardian_gate?.ok ? "open" : "blocked"],
            ["guardian reason", report.guardian_gate?.detail],
          ]} />
          {report.lab.error && <p className="failure">{report.lab.error}</p>}
          {(Object.entries(report.lab.expected ?? {})).map(([file, sha]) =>
            <div className="manifest-row" key={file}><button className="link" type="button"
              onClick={() => onOpenFile("lab", `${selected}/${file}`)}>{file}</button>
              <span className="mono">{sha.slice(0, 16)}…</span></div>)}
          <h4>Installed package</h4><KV rows={[
            ["approved manifest", report.promoted.expected_sha256 || "not promoted"],
            ["on-disk manifest", report.promoted.observed_sha256 || "not installed"],
            ["unchanged", report.promoted.unchanged ? "yes" : "no"],
          ]} />
          <h4>Recorded test output</h4>
          <pre className="log">{candidate?.evidence || "No test run recorded."}</pre>
          <p className="hint">{report.note}</p>
          <h4>Human review and promotion (CLI only)</h4>
          <p className="hint">Run these on the host where the Studio's runtime home exists,
            not in an agent task. First inspect source and tests above; fix any changed
            manifest and make failing tests pass. Establish a Guardian baseline as a
            human if the gate says none is recorded.</p>
          <pre className="command">{command("review")}{"\n"}
            {command("promote")} --approved-by {shellQuote("Your Name")}{"\n"}
            {command("deprecate")} --approved-by {shellQuote("Your Name")} --reason {shellQuote("superseded by a newer tool")}{"\n"}
            {command("rollback")} --reason {shellQuote("describe the defect")}</pre>
          <p className="hint">After promotion, start another task. The runtime loads
            approved tools at task startup; restart the backend to refresh its read-only
            tool catalog. Rollback is reflected on the next run.</p>
        </>}
      </div>
    </div>
  </div>;
}
