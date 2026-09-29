import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const DEFAULT_FRONTEND_BASE = 'http://127.0.0.1:5273';
const DEFAULT_API_BASE = 'http://127.0.0.1:8686';
const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const BROWSER = (process.env.KPI_CAPTURE_BROWSER ?? 'webkit').toLowerCase();

function _loopbackUrl(name: string, raw: string): URL {
	const url = new URL(raw);
	if (url.protocol !== 'http:') {
		throw new Error(`${name} must use http, got ${url.protocol}`);
	}
	if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') {
		throw new Error(`${name} must be loopback, got ${url.hostname}`);
	}
	if (url.port === '') {
		throw new Error(`${name} must include an explicit port`);
	}
	if (url.pathname !== '/' || url.search !== '' || url.hash !== '') {
		throw new Error(`${name} must be an origin without path, query, or fragment`);
	}
	return url;
}

const frontend = _loopbackUrl(
	'PERFORMANCE_E2E_BASE_URL',
	process.env.PERFORMANCE_E2E_BASE_URL ?? DEFAULT_FRONTEND_BASE
);
const api = _loopbackUrl(
	'PERFORMANCE_E2E_API_BASE',
	process.env.PERFORMANCE_E2E_API_BASE ?? DEFAULT_API_BASE
);

const projects =
	BROWSER === 'chromium'
		? [
				{
					name: 'kpi-s2-capture-chromium',
					use: {
						...devices['Desktop Chrome'],
						launchOptions: {
							args: ['--autoplay-policy=no-user-gesture-required']
						}
					}
				}
			]
		: [
				{
					name: 'kpi-s2-capture-webkit',
					use: { ...devices['Desktop Safari'] }
				}
			];

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'kpi-s2-capture.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: 120_000,
	expect: { timeout: 15_000 },
	use: {
		baseURL: frontend.origin,
		viewport: { width: 1280, height: 800 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects
});
