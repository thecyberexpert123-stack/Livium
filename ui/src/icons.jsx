import React from "react";

// Small, local icon set: no external font, image host, or network request is needed
// for the production chat UI. Buttons still carry readable text or aria-labels.
const shapes = {
  sparkles: <><path d="m12 2 1.8 5.2L19 9l-5.2 1.8L12 16l-1.8-5.2L5 9l5.2-1.8L12 2Z"/><path d="m19 16 .7 1.8L21.5 18.5l-1.8.7L19 21l-.7-1.8-1.8-.7 1.8-.7L19 16ZM4 16l.6 1.4L6 18l-1.4.6L4 20l-.6-1.4L2 18l1.4-.6L4 16Z"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
  sidebar: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></>,
  panel: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></>,
  search: <><circle cx="10.7" cy="10.7" r="6.7"/><path d="m16 16 4.5 4.5"/></>,
  chat: <path d="M20 11.5a8 8 0 0 1-8 8c-1.3 0-2.5-.3-3.6-.8L4 20l1.2-4.3A8 8 0 1 1 20 11.5Z"/>,
  folder: <path d="M3.5 6.5h6l2 2h9v9.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-11a.5.5 0 0 1 .5-.5Z"/>,
  file: <><path d="M6 3.5h7l5 5V20a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z"/><path d="M13 3.5V9h5"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></>,
  settings: <><circle cx="12" cy="12" r="3"/><path d="M9.1 3.8h5.8l.5 2.2 1.6.9 2.2-.6 2.1 3.6-1.6 1.6v1.8l1.6 1.6-2.1 3.6-2.2-.6-1.6.9-.5 2.2H9.1l-.5-2.2-1.6-.9-2.2.6-2.1-3.6 1.6-1.6v-1.8l-1.6-1.6 2.1-3.6 2.2.6 1.6-.9.5-2.2Z"/></>,
  layers: <><path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5"/></>,
  activity: <><path d="M3 12h4l2.5-6 4 12 2.5-6H21"/></>,
  moon: <path d="M20.5 14.7A8.5 8.5 0 0 1 9.3 3.5 8.5 8.5 0 1 0 20.5 14.7Z"/>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></>,
  arrowUp: <path d="M12 19V5m-6 6 6-6 6 6"/>,
  arrowRight: <path d="M4 12h16m-6-6 6 6-6 6"/>,
  arrowLeft: <path d="M20 12H4m6-6-6 6 6 6"/>,
  paperclip: <path d="m8 12.5 6-6a3 3 0 1 1 4.2 4.2l-8 8a5 5 0 0 1-7.1-7.1l8.5-8.5"/>,
  chevronDown: <path d="m5 9 7 7 7-7"/>,
  x: <path d="M5 5 19 19M19 5 5 19"/>,
  stop: <rect x="6" y="6" width="12" height="12" rx="2"/>,
  check: <path d="m4 12 5 5L20 6"/>,
  alert: <><circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.8 9A7.5 7.5 0 0 1 19 7l1 5M4 12l1 5a7.5 7.5 0 0 0 13.2-2"/></>,
  copy: <><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></>,
  wand: <><path d="m4 20 12-12 4 4L8 24M14 3l.5 1.5L16 5l-1.5.5L14 7l-.5-1.5L12 5l1.5-.5L14 3ZM20 2l.5 1.5L22 4l-1.5.5L20 6l-.5-1.5L18 4l1.5-.5L20 2Z"/></>,
  shield: <><path d="M12 2.5 20 6v6c0 4.5-3 7.7-8 9.5C7 19.7 4 16.5 4 12V6l8-3.5Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/></>,
  terminal: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/></>,
  eye: <><path d="M2.5 12S6 6 12 6s9.5 6 9.5 6-3.5 6-9.5 6S2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.5"/></>,
  upload: <><path d="M12 16V4m-4 4 4-4 4 4M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></>,
  trash: <><path d="M4 7h16M10 4h4m4 3-.7 13H6.7L6 7m4 4v5m4-5v5"/></>,
};

export function Icon({ name, size = 18, className = "", ...rest }) {
  return <svg aria-hidden="true" focusable="false" className={className} width={size} height={size}
    viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    strokeLinecap="round" strokeLinejoin="round" {...rest}>{shapes[name] ?? shapes.sparkles}</svg>;
}
