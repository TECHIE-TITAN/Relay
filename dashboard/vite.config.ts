import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard only reads: every /api request goes to the read-only FastAPI process
// (python -m relay.cli dashboard-api, port 8090).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8090", changeOrigin: true } },
  },
});
