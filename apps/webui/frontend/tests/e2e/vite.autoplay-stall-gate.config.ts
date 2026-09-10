/**
 * Vite dev server for the PLAY-08 AutoPlay stall-banner browser gate.
 *
 * Same shape as vite.comment-hotkey-gate.config.ts, and it needs a real
 * fixture library for the same reason: `/api/v1/preflight`'s
 * `library-attached` check holds the whole app on its "Starting up" screen for
 * a library-free data dir, so `/performance` never mounts and the banner under
 * test is never rendered.
 *
 * Ports are fixed and are not one of the reserved lane ports listed in
 * playwright.webkit-deckload.config.ts, nor the hotcue-mapping-gate pair
 * (8695, 5320), nor the comment-hotkey-gate pair (8696, 5321). `strictPort`
 * refuses to drift onto a neighbour's port instead of silently succeeding on
 * the wrong one.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const AUTOPLAY_STALL_GATE_FRONTEND_PORT = 5324;
export const AUTOPLAY_STALL_GATE_API_PORT = 8699;

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: AUTOPLAY_STALL_GATE_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${AUTOPLAY_STALL_GATE_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
