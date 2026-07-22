import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [sveltekit()],
  optimizeDeps: {
    // Prebundling rewrites Signalsmith's self-stringifying AudioWorklet
    // module and causes processor creation to time out.
    exclude: ['signalsmith-stretch'],
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8585',
    },
  },
});
