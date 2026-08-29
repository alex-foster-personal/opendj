import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const frontendPort = Number(process.env.STEMS_E2E_FRONTEND_PORT);
const apiOrigin = process.env.STEMS_E2E_API_BASE;

if (!Number.isSafeInteger(frontendPort) || apiOrigin === undefined) {
	throw new Error(
		'the stems e2e vite config needs STEMS_E2E_FRONTEND_PORT and STEMS_E2E_API_BASE'
	);
}

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: {
		// Same exclusion as vite.config.ts: the package serialises its own
		// module function into an AudioWorklet and esbuild pre-bundling
		// rewrites it, after which no deck ever loads.
		exclude: ['signalsmith-stretch']
	},
	server: {
		host: '127.0.0.1',
		port: frontendPort,
		strictPort: true,
		proxy: {
			// ws: true is load-bearing here, not boilerplate. The whole point of
			// this suite is a bar fed by the jobs.updated topic over
			// /api/v1/events; without WS upgrade the page falls back to its
			// 60s poll and the test measures nothing it means to.
			'/api': { target: apiOrigin, changeOrigin: false, ws: true }
		}
	}
});
