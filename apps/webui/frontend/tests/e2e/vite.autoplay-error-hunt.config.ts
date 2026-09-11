/**
 * Vite dev server for the AutoPlay / mixing error hunt (#1853).
 *
 * Same shape as vite.autoplay-stall-gate.config.ts, plus the
 * `signalsmith-stretch` optimizeDeps exclusion the /performance page needs
 * (without it the stretch worklet never posts ready and decks throw). Hunt
 * audio comes from the engine's ingested fixture wavs, not a tone-fixture
 * plugin.
 *
 * Ports are fixed and are not one of the reserved lane ports listed in
 * playwright.webkit-deckload.config.ts, nor the stall-gate pair (5324, 8699).
 * `strictPort` refuses to drift onto a neighbour's port instead of silently
 * succeeding on the wrong one.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const AUTOPLAY_HUNT_FRONTEND_PORT = 5326;
export const AUTOPLAY_HUNT_API_PORT = 8703;

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: {
		// The package serializes its own module function into an AudioWorklet.
		// Esbuild pre-bundling rewrites that function and the processor never
		// posts its ready message, so this exclusion is a runtime requirement.
		exclude: ['signalsmith-stretch']
	},
	server: {
		host: '127.0.0.1',
		port: AUTOPLAY_HUNT_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${AUTOPLAY_HUNT_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
