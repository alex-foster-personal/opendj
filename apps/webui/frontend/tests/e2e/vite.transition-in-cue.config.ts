/**
 * Vite dev server for the transition-in cue browser smoke (AGENT-51).
 *
 * Same shape as vite.autoplay-stall-gate.config.ts. Ports are fixed, clear of
 * every other gate's pair, and `strictPort` refuses to drift onto a neighbour.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const TRANSITION_IN_CUE_FRONTEND_PORT = 5334;
export const TRANSITION_IN_CUE_API_PORT = 8714;

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: TRANSITION_IN_CUE_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${TRANSITION_IN_CUE_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
