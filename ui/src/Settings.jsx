import React, { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get, write } from "./api.js";
import { Badge, Failure, KV } from "./common.jsx";

function parseExtra(text) {
  const extra = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const index = trimmed.indexOf("=");
    if (index < 0) throw new Error(`"${trimmed}" is not NAME=value`);
    extra[trimmed.slice(0, index).trim()] = trimmed.slice(index + 1).trim();
  }
  return extra;
}

export function Settings({ state, onState, onError }) {
  const [form, setForm] = useState({ provider: "openai_compatible", model: "", base_url: "",
                                      api_key: "", extra: "" });
  const [dirty, setDirty] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [note, setNote] = useState("");
  const [validation, setValidation] = useState(null);
  const [busy, setBusy] = useState(false);
  const backendLog = useQuery({ queryKey: ["backend-log"],
    queryFn: () => get("/studio/api/backend/log"), refetchInterval: 8000 });
  const settings = state?.settings;
  useEffect(() => {
    if (!settings || dirty) return;
    setForm({ provider: settings.provider, model: settings.model,
              base_url: settings.base_url, api_key: "",
              extra: Object.keys(settings.extra ?? {}).map(key =>
                `# ${key} is set (value is write-only). Add ${key}=new-value to replace or ${key}= to clear.`
              ).join("\n") });
  }, [settings, dirty]);
  const set = (field, value) => { setDirty(true); setForm(previous => ({ ...previous, [field]: value })); };

  async function save({ restart = true } = {}) {
    if (busy) return;
    setBusy(true); setNote(""); setValidation(null);
    try {
      const patch = { provider: form.provider, model: form.model, base_url: form.base_url,
                      extra: parseExtra(form.extra), restart };
      if (form.api_key) patch.api_key = form.api_key;
      const result = await write("/studio/api/settings", patch);
      setForm(previous => ({ ...previous, api_key: "" }));
      setDirty(false);
      setNote(result.note);
      setValidation(result.provider);
      await onState();
      backendLog.refetch();
    } catch (error) { setNote(`Not saved: ${error.message}`); onError(error.message); }
    finally { setBusy(false); }
  }
  async function clearKey() {
    if (!window.confirm("Remove the stored API key?")) return;
    try {
      const result = await write("/studio/api/settings", { clear_api_key: true, restart: true });
      setForm(previous => ({ ...previous, api_key: "" }));
      setNote(result.note); setValidation(result.provider);
      await onState();
    } catch (error) { onError(error.message); }
  }
  async function probe() {
    try {
      const next = await get("/studio/api/state?probe=1");
      setValidation(next.provider); onState(next);
      setNote("Fresh provider probe recorded by the runtime.");
    } catch (error) { onError(error.message); }
  }
  async function restart() {
    try {
      const result = await write("/studio/api/backend/restart", {});
      setValidation(result.provider); setNote("Runtime restarted; see its actual probe below.");
      await onState(); backendLog.refetch();
    } catch (error) { onError(error.message); }
  }
  const provider = validation ?? state?.provider ?? {};

  return <div className="sheet-body">
    <div className="card wide"><h4>Provider</h4>
      <p className="hint">The key is written to <code>{state?.studio?.home ?? "<studio home>"}/settings.json</code>
        with mode 0600 and passed to the runtime in its environment. The API never returns
        the value; the home browser also refuses that file, including raw download.</p>
      <form className="settings-grid" onSubmit={event => { event.preventDefault(); save(); }}>
        <label>provider
          <select value={form.provider} onChange={event => set("provider", event.target.value)}>
            <option value="openai_compatible">openai_compatible (OpenAI or custom)</option>
            <option value="anthropic">anthropic</option>
            <option value="ollama">ollama (local)</option>
            <option value="scripted">scripted (test double; no real model)</option>
          </select>
        </label>
        <label>model <input value={form.model} onChange={event => set("model", event.target.value)}
          placeholder="gpt-4o-mini / claude / llama3.1" /></label>
        <label>base URL <input value={form.base_url} onChange={event => set("base_url", event.target.value)}
          placeholder="https://api.openai.com/v1 (blank = default)" /></label>
        <label className="wide">API key
          <span className="row"><input aria-label="API key" type={showKey ? "text" : "password"}
            autoComplete="off" value={form.api_key} onChange={event => set("api_key", event.target.value)}
            placeholder={settings?.api_key_set
              ? `a key is set (${settings.api_key_hint}); enter a new one to replace it`
              : "enter a key; it is never echoed back"} />
            <button type="button" className="ghost" onClick={() => setShowKey(!showKey)}>
              {showKey ? "hide" : "show"}</button>
            <button type="button" className="ghost" onClick={clearKey}>clear</button></span>
        </label>
        <div className="row wide">
          <button type="submit" disabled={busy}>Save and restart the runtime</button>
          <button type="button" className="ghost" disabled={busy} onClick={() => save({ restart: false })}>
            Save only</button>
          <button type="button" className="ghost" disabled={busy} onClick={() => save()}>
            Validate against the provider</button>
          <button type="button" className="ghost" onClick={probe}>
            Re-probe now, without restarting</button>
        </div>
      </form>
      {note && <p className="hint" role="status">{note}</p>}
    </div>
    <div className="grid-3">
      <div className="card"><h4>What the runtime says</h4><KV rows={[
        ["configured", `${settings?.provider || "unknown"}${settings?.model ? ` / ${settings.model}` : ""}`],
        ["base url", settings?.base_url || "(provider default)"],
        ["api key", settings?.api_key_set ? `${settings.api_key_hint} · sha256 ${state?.key_fingerprint}` : "not set"],
        ["reachable", provider.ok == null ? "not probed" : String(provider.ok)],
        ["probe", provider.age_seconds == null ? "age unknown" : provider.fresh
          ? "taken just now" : `cached, taken ${provider.age_seconds}s ago`],
        ["detail", provider.detail],
        ["context window", provider.context_window && `${provider.context_window} (${provider.context_window_source})`],
        ["test double", provider.is_test_double === true ? "yes — no real model output" : "no"],
        ["isolation", state?.runtime?.isolation?.detail],
        ["runtime home", state?.studio?.home],
      ]} /><p className="hint">A probe is a real request. A failed probe is never
        silently replaced with the scripted double.</p></div>
      <div className="card"><h4>Extra environment</h4>
        <p className="hint">One <code>NAME=value</code> per line. Existing variable names
          are shown as comments; their values are write-only, so saving another setting
          does not erase them. Use <code>NAME=</code> to clear one. Test-double changes
          are called out in the health bar.</p>
        <textarea rows={6} aria-label="Extra environment" value={form.extra}
          onChange={event => set("extra", event.target.value)}
          placeholder={"LIVIUM_MAX_STEPS=12\nLIVIUM_FOLLOWUP_MAX_MODEL_CALLS=4"} /></div>
    </div>
    <div className="card wide"><h4>Runtime backend log</h4>
      <Failure error={backendLog.error} context="Backend log unavailable" />
      <pre className="log">{backendLog.data?.log || "(the runtime has logged nothing yet)"}</pre>
      <div className="row"><button className="ghost" type="button" onClick={restart}>
        restart the runtime</button><button type="button" className="link"
        onClick={() => backendLog.refetch()}>reload log</button></div>
    </div>
  </div>;
}
