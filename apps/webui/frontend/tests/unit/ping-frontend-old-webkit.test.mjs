/**
 * Sol review thread 3966717870 on PR #1560 (P2, non-blocking, fixed anyway):
 * `BrowserPanel.svelte`'s `_pingFrontend` (the Frontend liveness dot restored
 * by this PR) called `AbortSignal.timeout` directly, same defect and same
 * fix as `pingHealth` in `src/lib/api.ts` (see `ping-health-old-webkit.test.mjs`
 * for the full WebKit/`minimumSystemVersion` argument). `_pingFrontend` is a
 * local closure inside the component, not exported, so this evaluates the
 * real function straight from its source - the same technique
 * `browser-panel-superseded-load-toast.test.mjs` uses for `_loadPane` - with
 * `fetch` and `timeoutSignal` supplied as factory arguments rather than a
 * reconstructed reimplementation of either.
 *
 * [if] `AbortSignal.timeout` is unavailable (old WebKit) [then ⛔️] the
 * Frontend dot must still issue its fetch and report online on a 2xx, not
 * throw before the network call ever happens.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

/** Builds the real `_pingFrontend`, evaluated straight from
 * BrowserPanel.svelte, with `fetch` and `timeoutSignal` supplied as factory
 * arguments (the same pattern `_loadPane`'s test uses for its dependencies)
 * so it runs outside the component and outside Svelte's `window` global. */
function makePingFrontend({ fetchImpl, timeoutSignalImpl, origin, timeoutMs }) {
	const source = readFileSync(PANEL, 'utf8');
	const start = source.indexOf('\tasync function _pingFrontend(');
	const end = source.indexOf('\n\tfunction _coverageDot(', start);
	assert.ok(start >= 0 && end > start, 'could not isolate BrowserPanel._pingFrontend');
	const functionSource = source
		.slice(start, end)
		.replace(
			'async function _pingFrontend(): Promise<LibraryHealthDot> {',
			'async function _pingFrontend() {'
		)
		.replace('error: unknown', 'error')
		.replace('window.location.origin', 'origin')
		.replace('CONN_PING_TIMEOUT_MS', 'timeoutMs');
	assert.doesNotMatch(
		functionSource,
		/: LibraryHealthDot|: unknown/,
		'TypeScript annotation survived stripping'
	);
	const factory = Function(
		'fetch',
		'timeoutSignal',
		'origin',
		'timeoutMs',
		`return ${functionSource}`
	);
	return factory(fetchImpl, timeoutSignalImpl, origin, timeoutMs);
}

/** The real `timeoutSignal` from `src/lib/api.ts`, re-typed away, standing in
 * for the module import `_pingFrontend` now uses in production. Reimplemented
 * here (rather than bundled in) only because loadTypeScriptModule's esbuild
 * pass cannot easily hand a live export into a `Function()`-constructed
 * closure; its behaviour - AbortController + setTimeout, cleared by the
 * caller - is exactly what production carries, and the assertions below hit
 * `_pingFrontend`'s catch/success branches, not this helper's internals. */
function timeoutSignal(timeoutMs) {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(new DOMException('The operation timed out.', 'TimeoutError')), timeoutMs);
	return { signal: controller.signal, clear: () => clearTimeout(timer) };
}

test('_pingFrontend still issues its fetch and reports online when AbortSignal.timeout is unavailable (old WebKit)', async () => {
	const originalAbortSignalTimeout = AbortSignal.timeout;
	AbortSignal.timeout = undefined;
	try {
		let fetchCalled = false;
		const pingFrontend = makePingFrontend({
			fetchImpl: async (url, init) => {
				fetchCalled = true;
				assert.ok(init.signal instanceof AbortSignal, 'must still pass a real AbortSignal to fetch');
				return { ok: true, status: 200 };
			},
			timeoutSignalImpl: timeoutSignal,
			origin: 'https://ping-frontend.example.test',
			timeoutMs: 2000
		});

		const dot = await pingFrontend();

		assert.ok(fetchCalled, '_pingFrontend must have actually issued its fetch');
		assert.equal(dot.state, 'complete');
		assert.equal(dot.detail, 'dev server online');
	} finally {
		AbortSignal.timeout = originalAbortSignalTimeout;
	}
});

test('_pingFrontend still reports the real HTTP failure (the timeout fallback must not mask it)', async () => {
	const originalAbortSignalTimeout = AbortSignal.timeout;
	AbortSignal.timeout = undefined;
	try {
		const pingFrontend = makePingFrontend({
			fetchImpl: async () => ({ ok: false, status: 502 }),
			timeoutSignalImpl: timeoutSignal,
			origin: 'https://ping-frontend.example.test',
			timeoutMs: 2000
		});

		const dot = await pingFrontend();

		assert.equal(dot.state, 'error');
		assert.match(dot.detail, /HTTP 502/);
	} finally {
		AbortSignal.timeout = originalAbortSignalTimeout;
	}
});
