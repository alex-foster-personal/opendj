import type { Handle } from '@sveltejs/kit';

/**
 * r3919761144: dev-only head injection for the REFRESH-01 full-reload
 * receiver. `transformPageChunk` is SvelteKit's own supported hook for
 * modifying rendered HTML - `../vite-hold-full-reload.ts`'s previous
 * approach (a Vite `transformIndexHtml` plugin hook) never fired at all
 * under SvelteKit's dev middleware, which does not call
 * `server.transformIndexHtml()` (see dev-full-reload-receiver.ts's docstring
 * for how that was confirmed). `import.meta.env.DEV` is statically replaced
 * at build time, so this branch - and the `<script>` tag it emits - does not
 * exist in a production build; there is nothing here for `build/index.html`
 * to 404 on.
 */
export const handle: Handle = async ({ event, resolve }) => {
	if (!import.meta.env.DEV) return resolve(event);
	return resolve(event, {
		transformPageChunk: ({ html }) =>
			html.includes('</head>')
				? html.replace(
						'</head>',
						'<script type="module" src="/src/lib/rb/dev-full-reload-receiver.ts"></script></head>'
					)
				: html
	});
};
