import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发态：/api 与 WS 代理到本地后端（默认 8000；生产由后端/nginx 直接服务）
const backend = process.env.PLATFORM_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: backend,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 800,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
