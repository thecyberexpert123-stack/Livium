import React, { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { get, remove, upload, write, workspaceUrl } from "./api.js";
import { Sidebar } from "./Sidebar.jsx";
import { Conversation } from "./Conversation.jsx";
import { Workspace } from "./Workspace.jsx";
import { Journal } from "./Journal.jsx";
import { Settings } from "./Settings.jsx";
import { Capabilities } from "./Capabilities.jsx";
import "./styles.css";

function usePersisted(key, initial) {
  const [value, setValue] = useState(() => {
    try { return localStorage.getItem(key) ?? initial; } catch { return initial; }
  });
  useEffect(() => { try { localStorage.setItem(key, value); } catch { /* private mode */ } }, [key, value]);
  return [value, setValue];
}

export default function App() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState(null);
  const [pendingRun, setPendingRun] = useState(null);
  const [root, setRoot] = useState("workspace");
  const [directory, setDirectory] = useState("");
  const [selectedFile, setSelectedFile] = useState(null);
  const [sheet, setSheet] = useState(null);
  const [banner, setBanner] = useState("");
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  const [resetToken, setResetToken] = useState(0);
  const [theme, setTheme] = usePersisted("livium.theme", "light");
  const [sidebarValue, setSidebarValue] = usePersisted("livium.sidebar",
    typeof window !== "undefined" && window.innerWidth <= 760 ? "hidden" : "shown");
  // Chat is the primary surface; the real workspace is a drawer, not a panel
  // consuming a third of every new conversation. A user's saved choice persists.
  const [dockValue, setDockValue] = usePersisted("livium.dock", "hidden");
  const sidebarOpen = sidebarValue !== "hidden";
  const dockOpen = dockValue !== "hidden";
  const wasRunning = useRef(false);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content",
      theme === "dark" ? "#171820" : "#ffffff");
  }, [theme]);
  useEffect(() => {
    const onKey = event => {
      if (event.key !== "Escape") return;
      if (sheet) setSheet(null);
      else if (dockOpen) setDockValue("hidden");
      else if (window.innerWidth <= 760 && sidebarOpen) setSidebarValue("hidden");
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sheet, dockOpen, sidebarOpen, setDockValue, setSidebarValue]);
  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => setToast(""), 4300);
    return () => clearTimeout(timer);
  }, [toast]);

  const stateQuery = useQuery({ queryKey: ["state"], queryFn: () => get("/studio/api/state"),
    refetchInterval: 8000, retry: false });
  const tasksQuery = useQuery({ queryKey: ["tasks"],
    queryFn: () => get("/studio/api/chat?limit=60"), refetchInterval: 8000, retry: false });
  const tasks = tasksQuery.data?.tasks ?? [];
  const row = tasks.find(item => item.task_id === selectedId);
  const taskQuery = useQuery({ queryKey: ["task", selectedId],
    queryFn: () => get(`/studio/api/chat/${encodeURIComponent(selectedId)}`),
    enabled: Boolean(selectedId), retry: false,
    refetchInterval: query => query.state.data?.run?.running ||
      (pendingRun?.running && pendingRun?.task_id === selectedId) ? 1500 : 8000 });
  const detail = selectedId ? taskQuery.data : null;
  const justStarted = pendingRun?.task_id === selectedId ? pendingRun : null;
  // A resumed task can already have an older run on its row. Show/poll the new
  // process rather than accidentally switching back to the old run's exit status.
  const runId = justStarted?.id ?? detail?.run?.id ?? row?.run?.id;
  const runQuery = useQuery({ queryKey: ["run", runId],
    queryFn: () => get(`/studio/api/runs/${encodeURIComponent(runId)}`),
    enabled: Boolean(runId), retry: false,
    refetchInterval: query => query.state.data?.run?.running !== false ? 1500 : false });
  const run = runQuery.data?.run ?? justStarted ?? detail?.run ?? row?.run;
  const workspaceQuery = useQuery({ queryKey: ["workspace", root, directory],
    queryFn: () => get(`/studio/api/workspace?root=${encodeURIComponent(root)}` +
                        `&path=${encodeURIComponent(directory)}`),
    refetchInterval: 8000, retry: false });
  const recentQuery = useQuery({ queryKey: ["recent", root],
    queryFn: () => get(`/studio/api/workspace/recent?root=${encodeURIComponent(root)}&minutes=180`),
    refetchInterval: 8000, retry: false });
  const fileQuery = useQuery({ queryKey: ["file", root, selectedFile],
    queryFn: () => get(workspaceUrl(root, selectedFile)),
    enabled: Boolean(selectedFile), retry: false });

  useEffect(() => {
    if (run?.running) wasRunning.current = true;
    else if (run && wasRunning.current) {
      wasRunning.current = false;
      setPendingRun(null);
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task", selectedId] });
      queryClient.invalidateQueries({ queryKey: ["workspace"] });
      queryClient.invalidateQueries({ queryKey: ["recent"] });
      queryClient.invalidateQueries({ queryKey: ["capabilities"] });
      setToast("Run finished — the transcript and verdict now come from the runtime record.");
    }
  }, [run?.running, run?.id, selectedId, queryClient]);

  async function onSubmit(body) {
    setBusy(true); setBanner("");
    try {
      const started = await write("/studio/api/chat", body);
      setPendingRun(started.run);
      setSelectedId(started.run.task_id);
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task", started.run.task_id] });
      setToast("Started a real Livium run; waiting for its first journal entry.");
      return started.run;
    } finally { setBusy(false); }
  }
  async function onStop() {
    if (!run?.id) return;
    try {
      await write(`/studio/api/runs/${encodeURIComponent(run.id)}/stop`, {});
      setToast("Stopped the run process; inspect the journal for its actual outcome.");
      queryClient.invalidateQueries({ queryKey: ["run", run.id] });
      queryClient.invalidateQueries({ queryKey: ["task", selectedId] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    } catch (error) { setBanner(error.message); }
  }
  function openFile(chosenRoot, path) {
    setRoot(chosenRoot);
    setDirectory(path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
    setSelectedFile(path);
    setDockValue("shown");
  }
  function navigate(chosenRoot, folder) {
    setRoot(chosenRoot); setDirectory(folder); setSelectedFile(null);
  }
  async function uploadFiles(files) {
    if (!files?.length) return;
    try {
      await upload(root, directory, files);
      setToast(`Uploaded ${files.length} file(s) into ${directory || root}.`);
      queryClient.invalidateQueries({ queryKey: ["workspace", root] });
      queryClient.invalidateQueries({ queryKey: ["recent", root] });
    } catch (error) { setBanner(`Upload failed: ${error.message}`); }
  }
  async function attachFiles(files) {
    // Attachments are actual workspace uploads, not blobs kept only in the UI.
    // The composer uses the accepted server paths when it names them to the agent.
    const placed = await upload("workspace", "", files);
    queryClient.invalidateQueries({ queryKey: ["workspace", "workspace"] });
    queryClient.invalidateQueries({ queryKey: ["recent", "workspace"] });
    return placed;
  }
  async function newFolder() {
    const name = window.prompt("Folder name");
    if (!name) return;
    try {
      await write(`/studio/api/workspace/dir?root=${encodeURIComponent(root)}`,
                  { path: [directory, name].filter(Boolean).join("/") });
      queryClient.invalidateQueries({ queryKey: ["workspace", root] });
    } catch (error) { setBanner(`Could not create folder: ${error.message}`); }
  }
  async function deleteEntry() {
    const relative = selectedFile || directory;
    if (!relative) return;
    if (!window.confirm(`Delete ${relative} from ${root}? This changes real files.`)) return;
    const url = `/studio/api/workspace/entry?root=${encodeURIComponent(root)}` +
                `&path=${encodeURIComponent(relative)}`;
    try {
      try { await remove(url); }
      catch (error) {
        if (error.status !== 409 || !window.confirm(`${relative} is not empty. Delete it recursively?`)) throw error;
        await remove(url + "&recursive=1");
      }
      if (selectedFile) setSelectedFile(null);
      else setDirectory(directory.split("/").slice(0, -1).join("/"));
      queryClient.invalidateQueries({ queryKey: ["workspace", root] });
      queryClient.invalidateQueries({ queryKey: ["recent", root] });
      setToast(`Removed ${relative}.`);
    } catch (error) { setBanner(`Delete failed: ${error.message}`); }
  }
  // Connection problems have a dedicated, actionable state in the chat header.
  // Reserve the red banner for operation errors; don't bury every conversation
  // beneath a repeated probe failure or a test-double warning.
  const combinedBanner = [banner, stateQuery.error && `Studio unavailable: ${stateQuery.error.message}`,
    stateQuery.data?.error,
    stateQuery.data?.backend?.last_error &&
      `Runtime backend failed to start: ${stateQuery.data.backend.last_error}`]
    .filter(Boolean).join("\n\n");
  const taskError = taskQuery.error && !(taskQuery.error.status === 404 && run?.running)
    ? taskQuery.error : null;
  function newTask() {
    setSelectedId(null); setPendingRun(null); setResetToken(previous => previous + 1);
    setBanner(""); setSheet(null); setDockValue("hidden");
    if (window.innerWidth <= 760) setSidebarValue("hidden");
  }
  function selectTask(id) {
    wasRunning.current = false;
    setSelectedId(id);
    setPendingRun(null);
    if (window.innerWidth <= 760) setSidebarValue("hidden");
  }
  async function refreshState(payload) {
    if (payload) { queryClient.setQueryData(["state"], payload); return; }
    await stateQuery.refetch();
    queryClient.invalidateQueries({ queryKey: ["tasks"] });
  }
  return <div className={`app ${sidebarOpen ? "sidebar-open" : "sidebar-hidden"}
                              ${dockOpen ? "dock-open" : "dock-hidden"}`}>
    <Sidebar tasks={tasks} selectedId={selectedId} state={stateQuery.data}
      error={tasksQuery.error} onSelect={selectTask} onNew={newTask}
      onRefresh={() => tasksQuery.refetch()} onOpenSheet={setSheet}
      theme={theme} onTheme={() => setTheme(theme === "dark" ? "light" : "dark")}
      onHide={() => setSidebarValue("hidden")} />
    {sidebarOpen && <button className="sidebar-backdrop" type="button"
      aria-label="Close conversations drawer" onClick={() => setSidebarValue("hidden")} />}
    <Conversation selectedId={selectedId} detail={detail} error={taskError}
      state={stateQuery.data} run={run} banner={combinedBanner} busy={busy} resetToken={resetToken}
      onOpenFile={openFile} onStop={onStop} onSubmit={onSubmit} onAttach={attachFiles}
      onError={setBanner} onNew={newTask} onOpenSettings={() => setSheet("settings")}
      onShowSidebar={() => { setSidebarValue("shown"); if (window.innerWidth <= 760) setDockValue("hidden"); }}
      onToggleDock={() => { setDockValue(dockOpen ? "hidden" : "shown");
        if (window.innerWidth <= 760) setSidebarValue("hidden"); }}
      dockOpen={dockOpen} />
    {dockOpen && <button className="dock-backdrop" type="button"
      aria-label="Close workspace drawer" onClick={() => setDockValue("hidden")} />}
    <Workspace roots={stateQuery.data?.studio?.roots} root={root} directory={directory}
      listing={workspaceQuery.data} listingError={workspaceQuery.error}
      recent={recentQuery.data} recentError={recentQuery.error} selected={selectedFile}
      file={fileQuery.data} fileError={fileQuery.error} onNavigate={navigate}
      onOpen={openFile} onUpload={uploadFiles} onNewFolder={newFolder}
      onDelete={deleteEntry} onRefresh={() => {
        workspaceQuery.refetch(); recentQuery.refetch();
      }} onClose={() => setDockValue("hidden")} />
    {sheet && <div className="overlay" role="presentation" onMouseDown={event => {
      if (event.target === event.currentTarget) setSheet(null);
    }}><section className="sheet" role="dialog" aria-modal="true"
      aria-label={sheet === "journal" ? "Journal" : sheet === "settings" ? "Settings" : "Capabilities"}>
      <header className="sheet-head"><strong>{sheet === "journal" ? "Journal" : sheet === "settings"
        ? "Settings" : "Capabilities"}</strong>
        <span className="sub">{sheet === "journal" ? "the runtime's own records" : sheet === "settings"
          ? "provider, model, API key and process state" : "the real self-extension lifecycle"}</span>
        <button className="icon-btn" type="button" aria-label="Close panel" title="Close panel"
          onClick={() => setSheet(null)}>✕</button></header>
      {sheet === "journal" && <Journal onOpenCapabilities={() => setSheet("capabilities")} />}
      {sheet === "settings" && <Settings state={stateQuery.data} onState={refreshState}
        onError={setBanner} />}
      {sheet === "capabilities" && <Capabilities state={stateQuery.data} onOpenFile={(chosen, path) => {
        openFile(chosen, path); setSheet(null);
      }} />}
    </section></div>}
    {toast && <p className="toast" role="status">{toast}</p>}
  </div>;
}
