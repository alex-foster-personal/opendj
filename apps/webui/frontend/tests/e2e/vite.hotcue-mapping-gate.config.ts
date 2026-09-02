/**
 * Vite dev server for the hot-cue mapping gate e2e suite (#736). Proxies
 * /api to the real backend booted by playwright.hotcue-mapping-gate.config.ts
 * against a throwaway unmapped-track fixture. No tone-fixture plugin needed:
 * this suite never measures audio, only the hot-cue pad's rendered state.
 *
 * Ports are fixed and explicit, never one of the reserved lane ports listed
 * in playwright.webkit-deckload.config.ts (8585, 5173, 8685, 9405, 8682,
 * 9402, 5216, 9473, 8688, 9408, 5273, 8686, 5399, 5311, 8690, 8692) or this
 * worktree's own claimed pair. ``strictPort`` refuses to drift onto a
 * neighbour's port instead of silently succeeding on the wrong one.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const HOTCUE_MAPPING_GATE_FRONTEND_PORT = 5320;
export const HOTCUE_MAPPING_GATE_API_PORT = 8695;

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: HOTCUE_MAPPING_GATE_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${HOTCUE_MAPPING_GATE_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
