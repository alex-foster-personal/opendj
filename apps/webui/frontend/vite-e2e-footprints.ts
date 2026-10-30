import { appendFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { Plugin, ViteDevServer } from 'vite';

/**
 * Records which `src/` modules the browser fetches from the dev server, one
 * line per request, so a root Playwright run can be turned into a per-spec
 * footprint (TEST-BUDGET round 3, scripts/ci_e2e_selection.py).
 *
 * Why a request log and not `page.coverage`: the root suite's 176 spec files
 * import `@playwright/test` directly, so a coverage fixture would have to be
 * threaded through every one of them. The dev server is the one place every
 * spec passes through. SSR is off (`src/routes/+layout.ts`), so every source
 * module that runs in a test is fetched by the browser as `/src/...`, and
 * every test gets a fresh browser context with an empty cache, so the fetch
 * repeats for every test. A module that is never requested never ran.
 *
 * On only when `E2E_FOOTPRINTS_DIR` is set (the CI root step and
 * `scripts/e2e_footprints_refresh.py run` set it); `pnpm dev` is untouched.
 * The log is `requests.jsonl` in that directory: `{"t": <ms since epoch>,
 * "path": "src/lib/x.ts"}` with the query string removed, so a Svelte
 * component's `?svelte&type=style` request and its module request both land
 * on the one path. `tests/e2e/support/footprint-reporter.ts` writes the
 * matching per-spec time windows, and the Python builder joins the two.
 */
export const E2E_FOOTPRINTS_DIR_VARIABLE = 'E2E_FOOTPRINTS_DIR';
export const REQUESTS_LOG = 'requests.jsonl';

function sourcePath(url: string): string | null {
	const bare = url.split('?', 1)[0];
	return bare.startsWith('/src/') ? bare.slice(1) : null;
}

export function e2eFootprintsPlugin(): Plugin[] {
	const dir = process.env[E2E_FOOTPRINTS_DIR_VARIABLE];
	if (!dir) return [];
	mkdirSync(dir, { recursive: true });
	const log = join(dir, REQUESTS_LOG);
	return [
		{
			name: 'mdt-e2e-footprints',
			apply: 'serve',
			configureServer(server: ViteDevServer) {
				server.middlewares.use((request, _response, next) => {
					const path = sourcePath(request.url ?? '');
					if (path !== null) {
						appendFileSync(log, JSON.stringify({ t: Date.now(), path }) + '\n');
					}
					next();
				});
			}
		}
	];
}
