// Vite's checked-in production output and the React source have one contract:
// the Node server must serve the *actual* hashed assets the page references,
// and the entrypoint must mount real components, not a static imitation.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { sourceDigest } from "../build-stamp.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const UI = path.resolve(HERE, "..");
const sourcePage = await fs.readFile(path.join(UI, "index.html"), "utf8");
const page = await fs.readFile(path.join(UI, "public", "index.html"), "utf8");
const entrypoint = await fs.readFile(path.join(UI, "src", "main.jsx"), "utf8");
const app = await fs.readFile(path.join(UI, "src", "App.jsx"), "utf8");
const styles = await fs.readFile(path.join(UI, "src", "styles.css"), "utf8");
const vite = await fs.readFile(path.join(UI, "vite.config.mjs"), "utf8");

function assetsOf(html) {
  return [...html.matchAll(/(?:src|href)="(\/static\/assets\/[^" ]+\.(?:js|css))"/g)]
    .map(match => match[1]);
}

test("the build output references real, served, hashed JS and CSS assets", async () => {
  const assets = assetsOf(page);
  assert.equal(assets.length, 2, "production page must reference one app bundle and its stylesheet");
  assert.equal(assets.some(asset => asset.endsWith(".js")), true);
  assert.equal(assets.some(asset => asset.endsWith(".css")), true);
  for (const asset of assets) {
    assert.match(asset, /index-[\w-]+\.(js|css)$/);
    const bytes = await fs.readFile(path.join(UI, "public", asset.replace("/static/", "")));
    assert.ok(bytes.length > 1000, `${asset} is empty; run npm run build`);
  }
});

test("the checked-in bundle was built from these sources, not an older UI", async () => {
  const stamped = (await fs.readFile(path.join(UI, "public", "source-hash.txt"), "utf8")).trim();
  assert.equal(stamped, await sourceDigest(), "source changed since the bundle was built; run npm run build");
});

test("the React entrypoint mounts into the same root in development and production", () => {
  assert.match(sourcePage, /id="root"/);
  assert.match(page, /id="root"/);
  assert.match(entrypoint, /createRoot\(document\.getElementById\("root"\)\)/);
  assert.match(entrypoint, /<App\s*\/>/);
  assert.match(app, /<Sidebar\s/);
  assert.match(app, /<Conversation\s/);
  assert.match(app, /<Workspace\s/);
  assert.match(app, /<Capabilities\s/);
});

test("build and dev call the Node API by relative URLs", () => {
  assert.match(vite, /base: command === "build" \? "\/static\/" : "\/"/);
  assert.match(vite, /"\/studio\/api"/);
  assert.match(vite, /"\/api"/);
  assert.match(app, /"\/studio\/api\/state"/);
  assert.doesNotMatch(app, /https?:\/\/127\.0\.0\.1|https?:\/\/localhost/);
});

test("no inline handlers or hardcoded server output in the HTML shell", () => {
  for (const html of [page, sourcePage]) {
    assert.doesNotMatch(html, /\son(click|change|submit|input|load)=/i);
    assert.doesNotMatch(html, /verified 1\/1|api_key|scripted-double/);
  }
});

test("the stylesheet covers all three responsive regions and the review sheet", () => {
  for (const selector of [".app", ".sidebar", ".chat", ".dock", ".composer",
                          ".transcript", ".sheet", ".toast", ".capability-grid"]) {
    assert.match(styles, new RegExp(`\\${selector}\\b`), `${selector} is not styled`);
  }
  assert.match(styles, /\[data-theme="dark"\]/);
  assert.match(styles, /@media \(max-width: 760px\)/);
});
