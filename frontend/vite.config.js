import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The frontend hits the Python backend via an absolute URL configured by
// VITE_API_URL (see .env.example). No proxy is needed in dev because CORS
// is enabled on the FastAPI backend with allow_origins=["*"] by default.
//
// If you'd rather hide the backend URL behind a same-origin path during
// dev, uncomment the `server.proxy` block below and set the frontend
// fetch URL back to '/api/search'.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // proxy: {
    //   '/api': {
    //     target: 'http://localhost:8000',
    //     changeOrigin: true,
    //     rewrite: (p) => p.replace(/^\/api/, ''),
    //   },
    // },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
