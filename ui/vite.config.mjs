import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { sourceDigest } from "./build-stamp.mjs";

// Two modes, one browser application. In development Vite serves the components
// with hot reload; in production the checked-in build is served by ui/server.mjs.
// Both call the SAME Node/Python APIs via same-origin relative URLs. The Vite proxy
// target is server-side only; no browser ever tries to call localhost directly.
export default defineConfig(({ command }) => ({
  plugins: [react(), {
    name: "livium-source-stamp",
    apply: "build",
    async generateBundle() {
      this.emitFile({ type: "asset", fileName: "source-hash.txt",
                      source: `${await sourceDigest()}\n` });
    },
  }],
  base: command === "build" ? "/static/" : "/",
  publicDir: false,
  build: { outDir: "public", emptyOutDir: true, assetsDir: "assets" },
  server: {
    host: "0.0.0.0",
    port: 5173,
    allowedHosts: true,
    proxy: {
      "/api": process.env.LIVIUM_STUDIO_PROXY || "http://127.0.0.1:8877",
      "/studio/api": process.env.LIVIUM_STUDIO_PROXY || "http://127.0.0.1:8877",
    },
  },
  test: {
    include: ["src/**/*.test.jsx"],
    environment: "jsdom",
    css: false,
  },
}));
