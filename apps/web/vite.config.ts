import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies the API rather than the client calling it across
// origins. That keeps one origin in development, so cookies and same-origin
// assumptions behave the way they will in production behind a single domain --
// and it means CORS is not silently load-bearing here.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
