/**
 * Vite server for the one-way-import-gate e2e. Self-contained on purpose.
 *
 * No API proxy and no backend: the spec fulfils every ``/api/v1/**`` request
 * itself, so the run proves the UI stays inert without a daemon anywhere near
 * a real rekordbox target.
 *
 * The port is fixed and explicit (never 5173/8585/8685/9405) and ``strictPort``
 * refuses to drift onto a neighbour's port instead of silently succeeding on
 * the wrong one.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const REKORDBOX_GATE_E2E_PORT = 5399;

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: { exclude: ['signalsmith-stretch'] },
	server: {
		host: '127.0.0.1',
		port: REKORDBOX_GATE_E2E_PORT,
		strictPort: true
	}
});
