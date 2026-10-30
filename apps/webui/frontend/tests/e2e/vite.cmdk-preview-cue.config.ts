/**
 * Vite dev server for the Cmd K cue-preview gate (CMDK-07..08). Proxies /api
 * to the real backend booted by playwright.cmdk-preview-cue.config.ts.
 *
 * Fixed ports, clear of every other e2e pair (see vite.comment-hotkey-gate.config.ts
 * for the list); ``strictPort`` refuses to drift onto a neighbor's port.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const CMDK_PREVIEW_CUE_FRONTEND_PORT = 5337;
export const CMDK_PREVIEW_CUE_API_PORT = 8717;

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: CMDK_PREVIEW_CUE_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${CMDK_PREVIEW_CUE_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
