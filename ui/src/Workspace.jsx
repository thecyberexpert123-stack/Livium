import React, { useEffect, useRef, useState } from "react";
import { imageBlobUrl, workspaceUrl } from "./api.js";
import { Failure, formatBytes, formatWhen } from "./common.jsx";

const RASTER_IMAGES = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"]);

function Preview({ root, file, selected, error }) {
  const [imageUrl, setImageUrl] = useState(null);
  const [imageError, setImageError] = useState(null);
  const extension = (file?.name?.split(".").pop() ?? "").toLowerCase();
  useEffect(() => {
    if (!selected || file?.kind !== "binary" || !RASTER_IMAGES.has(extension)) {
      setImageUrl(null);
      return;
    }
    let cancelled = false;
    let url;
    imageBlobUrl(root, selected).then(value => {
      url = value;
      if (!cancelled) setImageUrl(value);
      else URL.revokeObjectURL(value);
    }).catch(reason => { if (!cancelled) setImageError(reason.message); });
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [root, selected, file?.sha256, file?.kind, extension]);
  return <div className="dock-preview">
    <div className="dock-preview-head"><strong>{file?.name || "Select a file"}</strong>
      {file ? <span className="sub">{formatBytes(file.size)} · {formatWhen(file.modified)} · sha256
        <span className="mono"> {file.sha256?.slice(0, 16)}…</span> · <a className="link"
          href={workspaceUrl(root, selected, "&download=1")}>download</a></span>
        : <span className="sub">Preview, digest and download live here.</span>}</div>
    <Failure error={error} context="Cannot preview file" />
    {file?.kind === "text" && <pre className="preview">{file.content}
      {file.truncated ? "\n… (truncated for preview; download for the whole file)" : ""}</pre>}
    {file?.kind === "binary" && imageUrl && <img alt={`Preview of ${file.name}`} src={imageUrl} />}
    {imageError && <p className="failure" role="alert">Image preview: {imageError}</p>}
    {file?.kind === "binary" && !imageUrl && !imageError && <p className="hint">
      Binary file: download to view it. Only raster images get an inline preview;
      untrusted SVG and other active formats are download-only.</p>}
    {!selected && <pre className="preview">(nothing selected)</pre>}
  </div>;
}

export function Workspace({ roots, root, directory, listing, listingError, recent,
                            recentError, selected, file, fileError, onNavigate, onOpen,
                            onUpload, onNewFolder, onDelete, onRefresh, onClose }) {
  const input = useRef(null);
  const [dropping, setDropping] = useState(false);
  const knownRoots = Object.entries(roots ?? {});
  const base = listing?.root?.path ?? roots?.[root]?.path ?? root;
  const parent = directory.split("/").slice(0, -1).join("/");
  return <aside className="dock" aria-label="Workspace browser">
    <header className="dock-head"><strong>Workspace</strong>
      <select aria-label="Workspace root" value={root} onChange={e => onNavigate(e.target.value, "")}>
        {knownRoots.length ? knownRoots.map(([key, value]) =>
          <option key={key} value={key}>{value.label}</option>)
          : <option value="workspace">workspace</option>}
      </select>
      <button className="icon-btn" type="button" aria-label="Hide the workspace"
        title="Hide the workspace" onClick={onClose}>✕</button></header>
    <div className="dock-body">
      <div className="crumbs">{base}/{directory}</div>
      <div className="dock-actions">
        <button className="ghost" type="button" onClick={() => input.current?.click()}>Upload</button>
        <input ref={input} type="file" multiple hidden aria-label="Choose files to upload"
          onChange={event => { onUpload([...event.target.files]); event.target.value = ""; }} />
        <button className="ghost" type="button" onClick={onNewFolder}>New folder</button>
        <button className="ghost danger" type="button" disabled={!selected && !directory}
          onClick={onDelete}>Delete {selected ? "file" : "folder"}</button>
        <button className="link" type="button" aria-label="Reload workspace"
          onClick={onRefresh}>↻</button>
      </div>
      <div className={`drop ${dropping ? "over" : ""}`} role="button" tabIndex={0}
        onDragOver={event => { event.preventDefault(); setDropping(true); }}
        onDragLeave={() => setDropping(false)}
        onDrop={event => {
          event.preventDefault(); setDropping(false);
          if (event.dataTransfer?.files?.length) onUpload([...event.dataTransfer.files]);
        }} onClick={() => input.current?.click()}
        onKeyDown={event => { if (event.key === "Enter") input.current?.click(); }}>
        Drop files here to upload into this folder</div>
      <Failure error={listingError} context="Cannot list workspace" />
      <div className="file-list" aria-label="Files and folders">
        {directory && <button className="file" type="button" onClick={() => onNavigate(root, parent)}>
          <span>↰</span><span>..</span></button>}
        {!listingError && listing?.entries?.map(entry => <button
          className={`file ${selected === entry.path ? "selected" : ""}`}
          type="button" key={entry.path} onClick={() => entry.kind === "directory"
            ? onNavigate(root, entry.path) : onOpen(root, entry.path)}>
          <span>{entry.kind === "directory" ? "📁" : entry.kind === "symlink" ? "🔗" : "📄"}</span>
          <span>{entry.name}</span><span className="size">{entry.kind === "directory"
            ? "" : formatBytes(entry.size)}</span>
        </button>)}
        {!listingError && listing && !listing.entries?.length && <p className="empty">
          This folder is empty — upload a file to give the agent something to work with.</p>}
      </div>
      <div className="recent">
        <Failure error={recentError} context="Recent files unavailable" />
        {recent?.files?.length ? <>changed in the last 3 hours: {recent.files.slice(0, 6)
          .map(entry => <button key={entry.path} className="link" type="button"
            onClick={() => onOpen(root, entry.path)}>{entry.path}</button>)}</>
          : !recentError && "nothing changed in this root in the last 3 hours"}
      </div>
    </div>
    <Preview root={root} file={file} selected={selected} error={fileError} />
  </aside>;
}
