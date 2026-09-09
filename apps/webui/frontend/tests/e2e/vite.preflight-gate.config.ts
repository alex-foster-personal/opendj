/**
 * Vite dev server for the PREFLIGHT-01 boot gate e2e suite (issue #771).
 * Proxies /api to the real backend booted by
 * playwright.preflight-gate.config.ts, which starts this same vite config
 * TWICE (once per fixture) with the two ports below overridden by env vars,
 * so one file serves both the healthy and the broken arm.
 *
 * Ports are fixed and explicit, never one of the reserved lane ports this
 * repo's other e2e configs already claim (see
 * playwright.hotcue-mapping-gate.config.ts / playwright.boot-burst.config.ts
 * for the running list) or this worktree's own claimed pair.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const frontendPort = Number(process.env.PREFLIGHT_GATE_FRONTEND_PORT);
const apiPort = Number(process.env.PREFLIGHT_GATE_API_PORT);
if (!Number.isInteger(frontendPort) || !Number.isInteger(apiPort)) {
	throw new Error(
		'vite.preflight-gate.config.ts needs PREFLIGHT_GATE_FRONTEND_PORT and ' +
			'PREFLIGHT_GATE_API_PORT set (playwright.preflight-gate.config.ts sets both).'
	);
}

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: frontendPort,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${apiPort}`,
				changeOrigin: false
			}
		}
	}
});
