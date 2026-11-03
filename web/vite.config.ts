import { defineConfig } from 'vite';
import preact from '@preact/preset-vite';

// 构建产物落到 app/dashboard/dist，由 FastAPI 在 /dashboard 下提供（见 app/core/spa.py）。
// 开发时 `pnpm dev` 起在 5173，/api 等后端路径代理到本机 8000 的开发实例。
const backend = process.env.MYSPIDERS_BACKEND ?? 'http://127.0.0.1:8000';
const proxied = ['/api', '/push', '/timelog', '/dashboard/'].reduce<Record<string, object>>((acc, p) => {
  acc[p] = { target: backend, changeOrigin: false };
  return acc;
}, {});

export default defineConfig({
  base: '/dashboard/',
  plugins: [preact()],
  build: {
    outDir: '../app/dashboard/dist',
    emptyOutDir: true,
    target: 'es2022',
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: { uplot: ['uplot'] },
      },
    },
  },
  server: { port: 5173, proxy: proxied },
  test: { environment: 'jsdom' },
} as never);
