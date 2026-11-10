import { defineConfig, devices } from '@playwright/test';
import { kpiCaptureTestTimeoutMs } from './kpi-capture-timeouts.mjs';

const DEFAULT_FRONTEND_BASE = 'http://127.0.0.1:5273';

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

// No webServer: scripts/perf/js_heap_after_pane.py spawns the engine and passes loopback URLs.

export default defineConfig({
	captureGitInfo: { commit: false, diff: false },
	testDir: '.',
	testMatch: 'js-heap-after-pane.spec.ts',
	fullyParallel: false,
	workers: 1,
	retries: 0,
	timeout: kpiCaptureTestTimeoutMs(process.env.KPI_CAPTURE_TIMEOUT_S),
	expect: { timeout: 15_000 },
	use: {
		baseURL: frontend.origin,
		viewport: { width: 1280, height: 800 },
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure'
	},
	projects: [
		{
			// Bundled Chromium only: WebKit and Firefox have no HeapProfiler / Runtime.getHeapUsage.
			name: 'js-heap-after-pane-chromium',
			use: { ...devices['Desktop Chrome'] }
		}
	]
});
