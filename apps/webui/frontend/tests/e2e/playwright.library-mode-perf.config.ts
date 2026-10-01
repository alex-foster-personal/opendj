import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { kpiCaptureTestTimeoutMs } from './kpi-capture-timeouts.mjs';

const DEFAULT_FRONTEND_BASE = 'http://127.0.0.1:5273';
const DEFAULT_API_BASE = 'http://127.0.0.1:8686';
const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

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

export default defineConfig({
	// Off: on a pull_request CI run the default git fetch stalls webServer start (#4419).
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'library-mode-perf-capture.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: kpiCaptureTestTimeoutMs(process.env.KPI_CAPTURE_TIMEOUT_S),
	expect: { timeout: 15_000 },
	use: {
		baseURL: frontend.origin,
		viewport: { width: 1280, height: 800 },
		// Tracing records DOM snapshots in the very processes this capture
		// measures, so it would inflate both modes' footprint; the capture's
		// own KPI_CAPTURE_RESULT carries the failure reason instead.
		trace: 'off',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			name: 'library-mode-perf-capture-chromium',
			use: {
				...devices['Desktop Chrome'],
				channel: 'chrome',
				launchOptions: {
					ignoreDefaultArgs: ['--enable-automation'],
					args: [
						'--disable-blink-features=AutomationControlled',
						'--autoplay-policy=no-user-gesture-required'
					]
				}
			}
		}
	]
});
