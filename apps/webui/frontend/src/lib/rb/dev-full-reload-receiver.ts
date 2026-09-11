/**
 * r3919761144: receives the `mdt:full-reload` custom event
 * `../../vite-hold-full-reload.ts` rewrites Vite's own `full-reload` payload
 * into (see that file's docstring for the full trace of why the rewrite
 * exists at all).
 *
 * This used to be injected as an inline `<script type="module">` body via
 * that plugin's `transformIndexHtml` hook. Two things made that dead code
 * under SvelteKit, confirmed directly against this project's real dev
 * server: `@sveltejs/kit`'s dev middleware never calls
 * `server.transformIndexHtml()` at all (grepping the installed package's
 * source for the call finds nothing), so the hook never even ran; and
 * separately, an inline module script's BODY text - unlike a file requested
 * through a `src=` attribute - never receives Vite's `import.meta.hot`
 * injection in the first place, proven by embedding the same probe directly
 * in `app.html` and finding `typeof import.meta.hot` still `'undefined'`
 * there too. Either bug alone would have silently swallowed every Vite dev
 * full-reload this was meant to catch.
 *
 * This file is the fix: a real, separately-requested module, loaded via a
 * `<script type="module" src="...">` tag injected by `../../hooks.server.ts`
 * (dev only) instead - a genuine file request gets Vite's normal transform,
 * so `import.meta.hot` here is real. It loads independently of the SPA
 * bundle for the same reason the old approach tried to (r3918992964): the
 * browser requests this URL directly, not through app.js/entry.js, so it is
 * unaffected by whether the SvelteKit app itself currently compiles.
 */
// Single intersection cast, matching reload-countdown.ts's own
// installReloadCountdown() - erasing through `unknown` first here would
// count against the frontend.unknown_casts quality ratchet for no reason
// this file needs.
const w = window as Window & {
	__mdtScheduleReload?: (reason: string, seconds?: number) => void;
	__mdtFullReloadReceiverInstalled?: boolean;
};

if (import.meta.hot) {
	import.meta.hot.on('mdt:full-reload', (data: { path?: string } | undefined) => {
		if (typeof w.__mdtScheduleReload === 'function') {
			w.__mdtScheduleReload(data?.path ? `vite: ${data.path}` : 'vite dev reload');
		} else {
			location.reload();
		}
	});
	// Agent-native / e2e parity with reload-countdown.ts's own
	// window.__mdtScheduleReload: proves this exact module loaded as a real
	// (not inline-body) request AND that import.meta.hot was truthy here,
	// which is the precise defect r3919761144 reported.
	w.__mdtFullReloadReceiverInstalled = true;
}
