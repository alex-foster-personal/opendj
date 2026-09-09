import type { HMRPayload, Plugin, ViteDevServer } from 'vite';

/**
 * Holds a Vite dev full reload behind REFRESH-01's countdown instead of
 * letting it land immediately.
 *
 * `reload-countdown.ts`'s module docstring has the full trace: in Vite
 * 5.4.21, `client.mjs`'s `case "full-reload"` branch calls
 * `notifyListeners("vite:beforeFullReload", ...)` and then `pageReload()`
 * unconditionally on the next line, ~50ms later - nothing a browser-side
 * listener does can stop it, and `location.reload` cannot be patched either
 * (it is LegacyUnforgeable). So the hold has to happen before the message
 * ever reaches the client: this plugin wraps `server.hot.send` (the one
 * broadcaster every full-reload path in Vite core goes through - a changed
 * tsconfig, a client-dir file, an HMR propagation failure) and rewrites a
 * `full-reload` payload into a custom event instead of forwarding it.
 *
 * ## The catch-vs-app-code race (r3918992964)
 *
 * `installReloadCountdown()` (reload-countdown.ts) is called from the root
 * `+layout.svelte`'s `onMount`, which only fires once the WHOLE SPA bundle
 * has compiled, loaded and hydrated. A `full-reload` that Vite would send
 * BECAUSE the app currently fails to compile/start therefore arrives with
 * nobody registered to hear the custom event it was rewritten into - the
 * message that used to recover the page (native `pageReload()`) is silently
 * discarded instead, and the tab is stuck.
 *
 * ## r3919761144: the head-injection half of this used to live here too
 *
 * A `transformIndexHtml` hook is the standard Vite way to inject something
 * into every page's `<head>` independently of whether the SPA bundle
 * compiles - but `@sveltejs/kit`'s dev middleware never calls
 * `server.transformIndexHtml()` at all (confirmed by grepping the installed
 * package's source - no call site exists), so that hook was silent dead
 * code: it never ran, in this project or any SvelteKit project. The
 * `mdt:full-reload` receiver now lives in `src/lib/rb/dev-full-reload-
 * receiver.ts`, loaded via a `<script type="module" src="...">` tag that
 * `src/hooks.server.ts` injects (dev only) through SvelteKit's own supported
 * `transformPageChunk` hook - see both files' docstrings for the rest of the
 * trace, including why the receiver has to be a real file request (a `src=`
 * attribute) rather than an inline script body.
 */
export function holdFullReloadPlugin(): Plugin {
	return {
		name: 'mdt-hold-full-reload',
		configureServer(server: ViteDevServer) {
			const send = server.hot.send.bind(server.hot);
			server.hot.send = ((payload: HMRPayload) => {
				if (payload.type === 'full-reload') {
					send({ type: 'custom', event: 'mdt:full-reload', data: { path: payload.path } });
					return;
				}
				send(payload);
			}) as typeof server.hot.send;
		}
	};
}
