/**
 * Vite dev server for the REFRESH-01 full-reload gate (PR #890 review
 * r3920753724). No backend: the page under test never fetches anything this
 * suite cares about, the same way playwright.config.ts's own vite-only lane
 * already proves for `reload-countdown-browser.spec.ts`.
 *
 * This is its own config, not a test added to the shared playwright.config.ts,
 * because the whole point of the suite is to touch a REAL source file and let
 * Vite emit a REAL `full-reload` HMR message for it (`../../vite-hold-
 * full-reload.ts` intercepts and holds it). That broadcast goes to every
 * client connected to the dev server it runs against - every OTHER e2e spec
 * sharing playwright.config.ts's single vite instance would receive it too,
 * mid-test, since every page already has the same dev-only receiver script
 * injected by src/hooks.server.ts. A dedicated server on its own port is what
 * keeps that blast radius to this suite alone.
 *
 * Ports are fixed, never one of the reserved lane ports listed in
 * playwright.webkit-deckload.config.ts (8585, 5173, 8685, 9405, 8682, 9402,
 * 5216, 9473, 8688, 9408, 5273, 8686, 5399, 5311, 8690, 8692) or the
 * hotcue-mapping-gate (8695, 5320), comment-hotkey-gate (8696, 5321) or
 * rating-cell-fit-gate (8697, 5322) pairs. ``strictPort`` refuses to drift
 * onto a neighbour's port instead of silently succeeding on the wrong one.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

import { holdFullReloadPlugin } from '../../vite-hold-full-reload';

export const FULL_RELOAD_GATE_FRONTEND_PORT = 5323;

export default defineConfig({
	plugins: [sveltekit(), holdFullReloadPlugin()],
	server: {
		host: '127.0.0.1',
		port: FULL_RELOAD_GATE_FRONTEND_PORT,
		strictPort: true
	}
});
