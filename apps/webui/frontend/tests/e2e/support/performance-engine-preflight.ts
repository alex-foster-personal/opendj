/**
 * Fail closed when the performance harness is attached to a server that is not
 * the production engine (F10 / GB-6841).
 *
 * Playwright's webServer readiness probe waits on `/api/v1/health`, and the
 * legacy `apps.webui.server --prod` asset server answers that route while it
 * 404s the engine routes the integrated UI calls (`/api/v1/flags` among them).
 * So a run against the legacy server used to start cleanly and then fail on
 * the first spec that read engine state - a harness defect reported as a
 * product defect. This global setup runs after webServer boot and before any
 * spec, names the route that does not resolve, and refuses to continue.
 *
 * `PERFORMANCE_E2E_ENGINE=legacy` is the explicit opt-out: a caller who asked
 * for the legacy asset server gets a printed reason instead of a required
 * engine route, never a silent downgrade.
 */

const DEFAULT_API_BASE = 'http://127.0.0.1:8686';

/** Routes `apps.engine_core` serves and the legacy `--prod` server 404s. */
const ENGINE_ROUTES = ['/api/v1/health', '/api/v1/flags'] as const;

export default async function performanceEnginePreflight(): Promise<void> {
	const profile = process.env.PERFORMANCE_E2E_ENGINE ?? 'engine';
	const apiBase = (process.env.PERFORMANCE_E2E_API_BASE ?? DEFAULT_API_BASE).replace(/\/+$/, '');
	if (profile === 'legacy') {
		console.log(
			'performance e2e preflight: PERFORMANCE_E2E_ENGINE=legacy requested explicitly; engine routes are not required'
		);
		return;
	}
	const problems: string[] = [];
	for (const route of ENGINE_ROUTES) {
		let status: number;
		try {
			const response = await fetch(`${apiBase}${route}`);
			status = response.status;
			await response.body?.cancel();
		} catch (error) {
			problems.push(`${route} -> unreachable (${String(error)})`);
			continue;
		}
		if (status === 404 || status >= 500) problems.push(`${route} -> ${status}`);
	}
	if (problems.length > 0) {
		throw new Error(
			`performance e2e preflight: ${apiBase} does not serve the production engine routes (${problems.join(
				', '
			)}). The default ENGINE_CMD boots apps.engine_core serve; a legacy apps.webui.server --prod server answers /api/v1/health but not these, so the suite would measure a UI with no engine behind it. Start the real engine, or set PERFORMANCE_E2E_ENGINE=legacy to say you meant the legacy server.`
		);
	}
	console.log(
		`performance e2e preflight: ${ENGINE_ROUTES.length} engine routes resolve at ${apiBase}`
	);
}
