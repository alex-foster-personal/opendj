import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const apiProxyTarget = process.env.VITE_API_PROXY_TARGET
    ?? env.VITE_API_PROXY_TARGET
    ?? 'http://127.0.0.1:8585';
  return {
  plugins: [sveltekit()],
  optimizeDeps: {
    // Prebundling rewrites Signalsmith's self-stringifying AudioWorklet
    // module and causes processor creation to time out.
    exclude: ['signalsmith-stretch'],
  },
  server: {
    port: 5173,
    proxy: {
      '/api': apiProxyTarget,
    },
  },
  };
});
