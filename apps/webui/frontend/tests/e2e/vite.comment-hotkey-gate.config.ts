/**
 * Vite dev server for the comment-hotkey e2e suite (r3918992947). Proxies
 * /api to the real backend booted by playwright.comment-hotkey-gate.config.ts
 * against a throwaway, library-free data dir: the feedback routes only ever
 * touch <data-dir>/feedback/*.json, so no fixture audio/library is needed to
 * prove the real availability probe and the real hotkey handler together.
 *
 * Ports are fixed, never one of the reserved lane ports listed in
 * playwright.webkit-deckload.config.ts (8585, 5173, 8685, 9405, 8682, 9402,
 * 5216, 9473, 8688, 9408, 5273, 8686, 5399, 5311, 8690, 8692) or the
 * hotcue-mapping-gate pair (8695, 5320). ``strictPort`` refuses to drift onto
 * a neighbour's port instead of silently succeeding on the wrong one.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const COMMENT_HOTKEY_GATE_FRONTEND_PORT = 5321;
export const COMMENT_HOTKEY_GATE_API_PORT = 8696;

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: COMMENT_HOTKEY_GATE_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${COMMENT_HOTKEY_GATE_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
