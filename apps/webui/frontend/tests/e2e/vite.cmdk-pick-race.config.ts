/**
 * Vite dev server for the Cmd K pick-race gate (CMDK-16, issue #6091). Proxies
 * /api to the real backend booted by playwright.cmdk-pick-race.config.ts.
 *
 * Fixed ports, clear of every other e2e pair (see vite.comment-hotkey-gate.config.ts
 * for the list); ``strictPort`` refuses to drift onto a neighbor's port.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const CMDK_PICK_RACE_FRONTEND_PORT = 5336;
export const CMDK_PICK_RACE_API_PORT = 8716;

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: CMDK_PICK_RACE_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${CMDK_PICK_RACE_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
