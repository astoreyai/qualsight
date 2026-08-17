import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// The QualSight console is a local PWA that talks to the FastAPI control plane
// on the same workstation. No third-party origin is ever contacted.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["favicon.svg"],
      manifest: {
        name: "QualSight",
        short_name: "QualSight",
        description: "Biometric quality, recognition and evaluation workstation console",
        theme_color: "#0f172a",
        background_color: "#0f172a",
        display: "standalone",
        start_url: "/",
        icons: [
          { src: "icon-192.svg", sizes: "192x192", type: "image/svg+xml", purpose: "any" },
          { src: "icon-512.svg", sizes: "512x512", type: "image/svg+xml", purpose: "any" },
        ],
      },
      workbox: {
        // Never cache API responses or registered image bytes: results must
        // always come from the live control plane and its immutable store.
        navigateFallbackDenylist: [/^\/api\//],
        runtimeCaching: [],
      },
    }),
  ],
  server: {
    host: "127.0.0.1",
    port: 5180,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8480", changeOrigin: false } },
  },
  preview: {
    host: "127.0.0.1",
    port: 5181,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8480", changeOrigin: false } },
  },
  build: { sourcemap: true },
});
