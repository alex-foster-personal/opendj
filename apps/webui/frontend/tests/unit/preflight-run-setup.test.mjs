// requirement: PREFLIGHT-02
/**
 * PREFLIGHT-02 (issue #2589, #2653): a dismissed-empty library must not lock a
 * brand-new user behind a dead boot gate. The server-side half is
 * apps/webui/server/preflight_checks.py; this is the frontend half --
 * PreflightCheckRow must show a real, clickable "Run setup" control for the
 * `library-attached` row when (and only when) it comes back `pending` because
 * setup was dismissed with an empty library.
 *
 * SSR tests use mount-svelte.mjs; issue #2653 adds shouldShowPreflightStrip
 * truth-table coverage and a real client mount that clicks the control and
 * asserts dismiss POST then navigation to /setup.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';
import { bundlePreflightCheckRowClient } from './mount-svelte-client.mjs';
import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';
import { engineHealth, jsonResponse } from './setup-fixtures.mjs';

const API_BASE = 'https://preflight-run-setup.example.test';
const RUN_SETUP_TESTID = 'data-testid="preflight-run-setup"';

function check(overrides = {}) {
	return {
		id: 'engine-alive',
		label: 'Engine alive',
		status: 'pass',
		detail: 'the endpoint answered',
		remediation: null,
		...overrides
	};
}

/** The exact PreflightCheckOut row apps/webui/server/preflight_checks.py
 * produces for a fresh, dismissed-empty install's library-attached check. */
function dismissedEmptyLibraryRow() {
	return check({
		id: 'library-attached',
		label: 'Library attached',
		status: 'pending',
		detail: 'no state.db at /data/state/state.db yet; setup was dismissed with an empty library',
		remediation:
			'Setup was dismissed with an empty library. Click Run setup to import one, or keep using the app empty.'
	});
}

/** The true first-boot fail state: not dismissed, nothing imported yet. */
function freshFailLibraryRow() {
	return check({
		id: 'library-attached',
		label: 'Library attached',
		status: 'fail',
		detail: 'no state.db at /data/state/state.db',
		remediation: 'Run setup to import a library (Cmd+, > Run setup).'
	});
}

/** A fully attached, healthy library: nothing to run setup for. */
function attachedLibraryRow() {
	return check({ id: 'library-attached', label: 'Library attached', detail: '812 tracks' });
}

/** A `pending` row that is NOT library-attached (e.g. audio-access with
 * nothing sampleable) -- must never offer Run setup either. */
function pendingAudioAccessRow() {
	return check({
		id: 'audio-access',
		label: 'Audio access',
		status: 'pending',
		detail: 'no state.db yet; nothing to sample'
	});
}

/** SetupStatusOut, shaped like the real one. Returned by the dismiss write. */
function setupStatus(overrides = {}) {
	return {
		library_empty: true,
		tracks: 0,
		playlists: 0,
		state_db: { path: '/data/state/state.db', exists: false, size_bytes: 0, modified_at: null },
		data_dir: '/data',
		dismissed: false,
		dev_mode: false,
		should_show_wizard: false,
		stages: ['detect', 'snapshot', 'decrypt', 'ingest', 'analysis'],
		folder_stages: ['detect', 'scan', 'ingest'],
		last_import: null,
		rekordbox: null,
		permissions: null,
		...overrides
	};
}

function findByTestId(root, testId) {
	if (root.getAttribute?.('data-testid') === testId) return root;
	for (const child of root.childNodes ?? []) {
		const hit = findByTestId(child, testId);
		if (hit) return hit;
	}
	return null;
}

let bundle;
let bootCopy;
let clientBundle;
let teardownDom;
let originalFetch;

before(async () => {
	teardownDom = installQualityBadgeDom();
	bundle = await bundleSvelteEntry(`
		export { default as PreflightCheckRowComponent } from '$lib/components/preflight/PreflightCheckRow.svelte';
	`);
	bootCopy = await loadTypeScriptModule('src/lib/preflight/boot-copy.ts', {
		viteApiBase: API_BASE
	});
	clientBundle = await bundlePreflightCheckRowClient(API_BASE);
	originalFetch = globalThis.fetch;
});

after(() => {
	teardownDom?.();
	globalThis.fetch = originalFetch;
});

test('shouldShowPreflightStrip stays true after a dismissed-empty pass clears the gate (#2653)', () => {
	assert.equal(
		bootCopy.shouldShowPreflightStrip(false, true, [dismissedEmptyLibraryRow()]),
		true
	);
});

test('shouldShowPreflightStrip is false once library-attached passes', () => {
	assert.equal(
		bootCopy.shouldShowPreflightStrip(false, true, [attachedLibraryRow()]),
		false
	);
});

test('shouldShowPreflightStrip stays true while the gate is not cleared', () => {
	assert.equal(
		bootCopy.shouldShowPreflightStrip(false, false, [dismissedEmptyLibraryRow()]),
		true
	);
});

test('shouldShowPreflightStrip is false when the blocking gate shows', () => {
	assert.equal(bootCopy.shouldShowPreflightStrip(true, false, [dismissedEmptyLibraryRow()]), false);
});

test('dismissed-empty library-attached row renders a clickable, enabled Run setup button', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: dismissedEmptyLibraryRow(),
		navigate: () => {}
	});

	assert.ok(
		html.includes(RUN_SETUP_TESTID),
		'expected a preflight-run-setup control in the markup'
	);
	const buttonMatch = html.match(/<button[^>]*data-testid="preflight-run-setup"[^>]*>/);
	assert.ok(buttonMatch, 'expected a <button> element carrying the testid');
	assert.doesNotMatch(buttonMatch[0], /disabled/, 'the control must not be disabled in this state');
	assert.doesNotMatch(html, /not implemented - see PARITY-TODO/);
	assert.match(html, />\s*Run setup\s*</);
	assert.ok(html.includes('Setup was dismissed with an empty library'));
});

test('the true fresh-install fail row renders a clickable Run setup control (issue #2722)', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: freshFailLibraryRow(),
		navigate: () => {}
	});

	assert.ok(html.includes(RUN_SETUP_TESTID));
	const buttonMatch = html.match(/<button[^>]*data-testid="preflight-run-setup"[^>]*>/);
	assert.ok(buttonMatch);
	assert.doesNotMatch(buttonMatch[0], /disabled/);
});

test('an attached, healthy library row renders no Run setup control', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: attachedLibraryRow(),
		navigate: () => {}
	});

	assert.ok(!html.includes(RUN_SETUP_TESTID));
});

test('a pending row that is not library-attached renders no Run setup control', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: pendingAudioAccessRow(),
		navigate: () => {}
	});

	assert.ok(!html.includes(RUN_SETUP_TESTID));
});

test('clicking Run setup re-arms setup and navigates to /setup (issue #2653)', async () => {
	const calls = [];
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		calls.push(`${request.method} ${url.pathname}`);
		if (url.pathname === '/api/v1/health') return jsonResponse(engineHealth());
		if (url.pathname === '/api/v1/setup/dismiss') return jsonResponse(setupStatus());
		throw new Error(`unexpected request: ${request.method} ${url.pathname}`);
	};

	const { PreflightCheckRow, mount, unmount, flushSync } = clientBundle;
	const target = document.createElement('div');
	document.body.appendChild(target);
	const navigated = [];
	const instance = mount(PreflightCheckRow, {
		target,
		props: {
			check: dismissedEmptyLibraryRow(),
			navigate: (path) => navigated.push(path)
		}
	});

	try {
		const button = findByTestId(target, 'preflight-run-setup');
		assert.ok(button, 'expected preflight-run-setup button in the mounted tree');
		button.dispatchEvent({
			type: 'click',
			bubbles: true,
			cancelBubble: false,
			defaultPrevented: false,
			preventDefault() {},
			stopPropagation() {
				this.cancelBubble = true;
			}
		});

		for (let i = 0; i < 50 && navigated.length === 0; i += 1) {
			flushSync();
			await new Promise((r) => setTimeout(r, 0));
		}

		assert.deepEqual(navigated, ['/setup']);
		assert.ok(calls.includes('POST /api/v1/setup/dismiss'));
		const dismissIdx = calls.indexOf('POST /api/v1/setup/dismiss');
		const healthIdx = calls.indexOf('GET /api/v1/health');
		assert.ok(healthIdx < dismissIdx, 'health probe must precede dismiss write');
	} finally {
		unmount(instance);
		target.remove();
		globalThis.fetch = originalFetch;
	}
});

test('a failed dismiss write does not navigate and surfaces setupError', async () => {
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		if (url.pathname === '/api/v1/health') return jsonResponse(engineHealth());
		if (url.pathname === '/api/v1/setup/dismiss') {
			return jsonResponse({ detail: { code: 'boom', message: 'dismiss write failed' } }, 500);
		}
		throw new Error(`unexpected request: ${request.method} ${url.pathname}`);
	};

	const { PreflightCheckRow, mount, unmount, flushSync } = clientBundle;
	const target = document.createElement('div');
	document.body.appendChild(target);
	const navigated = [];
	const instance = mount(PreflightCheckRow, {
		target,
		props: {
			check: dismissedEmptyLibraryRow(),
			navigate: (path) => navigated.push(path)
		}
	});

	try {
		const button = findByTestId(target, 'preflight-run-setup');
		assert.ok(button);
		button.dispatchEvent({
			type: 'click',
			bubbles: true,
			cancelBubble: false,
			defaultPrevented: false,
			preventDefault() {},
			stopPropagation() {
				this.cancelBubble = true;
			}
		});

		for (let i = 0; i < 50; i += 1) {
			flushSync();
			await new Promise((r) => setTimeout(r, 0));
		}

		assert.deepEqual(navigated, []);
		const err = findByTestId(target, 'preflight-setup-error') ?? target.querySelector?.('.preflight-error');
		const errText = err?.textContent ?? '';
		assert.match(errText, /dismiss write failed|Something went wrong/);
	} finally {
		unmount(instance);
		target.remove();
		globalThis.fetch = originalFetch;
	}
});
