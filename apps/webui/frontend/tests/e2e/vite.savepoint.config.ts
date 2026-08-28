import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

import { requireLoopbackOrigin } from './savepoint-endpoints';

const frontend = requireLoopbackOrigin(
	'SAVEPOINT_SMOKE_BASE_URL',
	process.env.SAVEPOINT_SMOKE_BASE_URL
);
const api = requireLoopbackOrigin('SAVEPOINT_SMOKE_API_BASE', process.env.SAVEPOINT_SMOKE_API_BASE);

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: {
		// The package serializes its own module function into an AudioWorklet.
		// Esbuild pre-bundling rewrites that function and the processor never
		// posts its ready message, so every deck load times out without this.
		// Same exclusion as vite.config.ts and tests/e2e/vite.performance.config.ts.
		exclude: ['signalsmith-stretch']
	},
	server: {
		host: frontend.hostname,
		port: Number(frontend.port),
		strictPort: true,
		proxy: {
			'/api': {
				target: api.origin,
				changeOrigin: false
			}
		}
	}
});
