import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    proxy: Object.fromEntries(
      ['/health', '/settings', '/story', '/visual', '/music', '/music-credits', '/characters', '/backgrounds', '/ambience']
        .map(path => [path, 'http://127.0.0.1:8756'])
    ),
  },
});
