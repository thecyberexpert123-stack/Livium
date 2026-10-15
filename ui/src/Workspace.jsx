import React, { useEffect, useRef, useState } from "react";
import { imageBlobUrl, workspaceUrl } from "./api.js";
import { Failure, formatBytes, formatWhen } from "./common.jsx";
import { Icon } from "./icons.jsx";

const RASTER_IMAGES = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"]);

function Preview({ root, file, selected, error }) {
  const [imageUrl, setImageUrl] = useState(null);
  const [imageError, setImageError] = useState(null);
  const extension = (file?.name?.split(".").pop() ?? "").toLowerCase();
  useEffect(() => {
    setImageError(null);
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
    <div className="dock-preview-head"><span className="dock-label">FILE PREVIEW</span>
      <strong>{file?.name || "Select a file to preview"}</strong>
      {file ? <span className="sub">{formatBytes(file.size)} · {formatWhen(file.modified)} · sha256
        <span className="mono"> {file.sha256?.slice(0, 16)}…</span> · <a className="link"
          href={workspaceUrl(root, selected, "&download=1")}>download</a></span>
        : <span className="sub">Contents, digest and download show up here.</span>}</div>
    <Failure error={error} context="Cannot preview file" />
    {file?.kind === "text" && <pre className="preview">{file.content}
      {file.truncated ? "\n… (truncated for preview; download for the whole file)" : ""}</pre>}
    {file?.kind === "binary" && imageUrl && <img alt={`Preview of ${file.name}`} src={imageUrl} />}
    {imageError && <p className="failure" role="alert">Image preview: {imageError}</p>}
    {file?.kind === "binary" && !imageUrl && !imageError && <p className="hint">
      Binary file: download to view it. Only raster images get an inline preview;
      untrusted SVG and other active formats are download-only.</p>}
    {!selected && <div className="preview-empty"><Icon name="eye" size={24} />
      <span>Select a file above</span></div>}
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
    <header className="dock-head"><div className="dock-heading">
      <span className="dock-heading-icon"><Icon name="folder" size={20} /></span>
      <div><strong>Workspace</strong><small>FILES & ARTIFACTS</small></div></div>
      <button className="icon-btn" type="button" aria-label="Hide the workspace"
        title="Hide the workspace" onClick={onClose}><Icon name="x" size={18} /></button>
    </header>
    <div className="dock-body">
      <label className="root-picker">Browse
        <select aria-label="Workspace root" value={root} onChange={e => onNavigate(e.target.value, "")}>
          {knownRoots.length ? knownRoots.map(([key, value]) =>
            <option key={key} value={key}>{value.label}</option>)
            : <option value="workspace">workspace</option>}
        </select>
      </label>
      <div className="crumbs"><Icon name="folder" size={14} />{base}/{directory}</div>
      <div className="dock-actions">
        <button className="ghost" type="button" onClick={() => input.current?.click()}>
          <Icon name="upload" size={15} />Upload</button>
        <input ref={input} type="file" multiple hidden aria-label="Choose files to upload"
          onChange={event => { onUpload([...event.target.files]); event.target.value = ""; }} />
        <button className="ghost" type="button" onClick={onNewFolder}>
          <Icon name="plus" size={16} /> New folder</button>
        <button className="icon-btn" type="button" aria-label="Reload workspace"
          title="Reload workspace" onClick={onRefresh}><Icon name="refresh" size={17} /></button>
      </div>
      <div className={`drop ${dropping ? "over" : ""}`} role="button" tabIndex={0}
        onDragOver={event => { event.preventDefault(); setDropping(true); }}
        onDragLeave={() => setDropping(false)}
        onDrop={event => {
          event.preventDefault(); setDropping(false);
          if (event.dataTransfer?.files?.length) onUpload([...event.dataTransfer.files]);
        }} onClick={() => input.current?.click()}
        onKeyDown={event => { if (event.key === "Enter") input.current?.click(); }}>
        <Icon name="upload" size={21} /><span>Drop files here to upload</span></div>
      <Failure error={listingError} context="Cannot list workspace" />
      <div className="dock-section-head"><span>FILES</span>
        <button className="icon-btn danger" type="button" disabled={!selected && !directory}
          aria-label={`Delete ${selected ? "file" : "folder"}`} title="Delete selected file or folder"
          onClick={onDelete}><Icon name="trash" size={15} /></button></div>
      <div className="file-list" aria-label="Files and folders">
        {directory && <button className="file" type="button" onClick={() => onNavigate(root, parent)}>
          <Icon name="arrowLeft" size={16} /><span>..</span></button>}
        {!listingError && listing?.entries?.map(entry => <button
          className={`file ${selected === entry.path ? "selected" : ""}`}
          type="button" key={entry.path} onClick={() => entry.kind === "directory"
            ? onNavigate(root, entry.path) : onOpen(root, entry.path)}>
          <Icon name={entry.kind === "directory" ? "folder" : entry.kind === "symlink" ? "arrowRight" : "file"} size={17} />
          <span className="file-name">{entry.name}</span><span className="size">{entry.kind === "directory"
            ? "" : formatBytes(entry.size)}</span>
        </button>)}
        {!listingError && listing && !listing.entries?.length && <p className="empty">
          This folder is empty. Upload a file to give your agent something to work with.</p>}
      </div>
      <div className="recent"><span className="dock-label">CHANGED RECENTLY</span>
        <Failure error={recentError} context="Recent files unavailable" />
        {recent?.files?.length ? <div>{recent.files.slice(0, 6)
          .map(entry => <button key={entry.path} className="link" type="button"
            onClick={() => onOpen(root, entry.path)}>{entry.path}</button>)}</div>
          : !recentError && <span>Nothing changed in this root in the last 3 hours.</span>}
      </div>
    </div>
    <Preview root={root} file={file} selected={selected} error={fileError} />
  </aside>;
}
