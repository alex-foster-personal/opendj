/**
 * Vite dev server for the pairing PAIR-arrow browser spec (Codex P1, #6062).
 *
 * Same shape as vite.autoplay-error-hunt.config.ts. Its own fixed ports
 * (5332 / 8715), listed in apps/webui/port_config.py RESERVED_FIXED_PORTS, so
 * the dynamic allocator never hands them to another worktree. `strictPort`
 * refuses to drift onto a neighbour's port.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const PAIRING_ARROW_FRONTEND_PORT = 5332;
export const PAIRING_ARROW_API_PORT = 8715;

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: {
		// The stretch worklet must not be pre-bundled, or it never posts ready.
		exclude: ['signalsmith-stretch']
	},
	server: {
		host: '127.0.0.1',
		port: PAIRING_ARROW_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${PAIRING_ARROW_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
