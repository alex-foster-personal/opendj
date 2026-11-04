import type { HMRPayload, Plugin, ViteDevServer } from 'vite';

/** The single method every full-reload path in Vite broadcasts through. */
interface HotChannel {
	send: (payload: HMRPayload) => void;
}

/**
 * Holds a Vite dev full reload behind REFRESH-01's countdown instead of
 * letting it land immediately.
 *
 * `reload-countdown.ts`'s module docstring has the full trace: the client's
 * `case "full-reload"` branch calls `notifyListeners("vite:beforeFullReload",
 * ...)` and then `pageReload()` unconditionally on the next line, ~50ms later -
 * nothing a browser-side listener does can stop it, and `location.reload`
 * cannot be patched either (it is LegacyUnforgeable). So the hold has to happen
 * before the message ever reaches the client: this plugin rewrites an outgoing
 * `full-reload` payload into a custom event instead of forwarding it, so the
 * Vite client never sees a `full-reload` message at all and its unconditional
 * `pageReload()` never runs.
 *
 * ## Which object to wrap (measured against vite 6.4.3 this run)
 *
 * Wrapping `server.hot.send` alone is dead code on the installed Vite: every
 * `DevEnvironment` is given its OWN hot channel in its constructor
 * (`normalizeHotChannel(...)` / the raw WebSocket transport), and `server.hot`
 * is a SEPARATE normalised wrapper around the same transport. Measured on the
 * real dev server started from `tests/e2e/vite.full-reload-gate.config.ts`:
 * `server.hot === server.environments.client.hot` is `false`, and the plugin's
 * patched method reported the rewrite while a real `full-reload` still reached
 * the page (the gate spec observed the overlay never appearing and the page
 * reloading at once). The HMR propagation calls `environment.hot.send(...)`,
 * so the channel that actually broadcasts has to be the one patched. Both are
 * patched here - `server.hot` for any caller still routing through the
 * compatibility wrapper, every environment's own channel for Vite itself -
 * with a WeakSet so a channel shared by two owners is wrapped once and the
 * rewrite cannot recurse.
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
			const patched = new WeakSet<object>();

			const hold = (channel: HotChannel): void => {
				if (patched.has(channel)) return;
				patched.add(channel);
				const forward = channel.send.bind(channel);
				channel.send = ((payload: HMRPayload) => {
					if (payload.type === 'full-reload') {
						forward({
							type: 'custom',
							event: 'mdt:full-reload',
							data: { path: payload.path }
						});
						return;
					}
					forward(payload);
				}) as HotChannel['send'];
			};

			hold(server.hot);
			for (const environment of Object.values(server.environments)) hold(environment.hot);
		}
	};
}
