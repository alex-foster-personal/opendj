/**
 * The first-run gate: ONE gate, in the root layout, raising the setup OVERLAY
 * over the performance view.
 *
 * A brand new user landing on an empty table with no explanation is the
 * problem; being thrown onto a standalone /setup page before they have seen
 * the app is the overcorrection. The gate now raises a dialog over the app's
 * own front door, so the thing they just installed is visible underneath the
 * ask and the wizard is minimisable while a 10,000-track import runs.
 *
 * The decision is a pure function plus one async resolver, executed here for
 * real. Only the markup facts a node:test harness cannot render are pinned by
 * source shape, the way capability-gating-markup.test.mjs pins its own.
 *
 * Regression lines:
 * - if the gate moves back onto the library page then the ask exists on one
 *   route only, and never on the route the packaged app actually lands on
 * - if the gate stops honouring setupRefusal then a legacy boot fires a
 *   request at an endpoint that is guaranteed to 404
 * - if a failed status probe shows the overlay anyway then a transient error
 *   strands the user behind a dialog over a library that loaded fine
 * - if the overlay stops being fixed + full-inset then it no longer dims the
 *   app it is explaining
 * - if the overlay is opaque then the "see what you installed" point is gone
 * - if the overlay stops being a labelled dialog then no test and no screen
 *   reader can find the one surface a first-run user meets
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { engineHealth, jsonResponse } from './setup-fixtures.mjs';

const API_BASE = 'https://first-run.example.test';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

let mod;
let originalFetch;

/** The status payload, shaped like SetupStatusOut. Only the fields the gate
 * reads are asserted on; the rest are present so the model is a real one. */
function status(overrides = {}) {
	return {
		library_empty: true,
		tracks: 0,
		playlists: 0,
		state_db: { path: '/data/state/state.db', exists: false, size_bytes: null, modified_at: null },
		data_dir: '/data',
		dismissed: false,
		dev_mode: false,
		should_show_wizard: true,
		stages: ['detect', 'snapshot', 'decrypt', 'ingest', 'analysis'],
		folder_stages: ['detect', 'scan', 'ingest'],
		last_import: null,
		rekordbox: null,
		permissions: null,
		...overrides
	};
}

before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/first-run-entry.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => jsonResponse(engineHealth());
	assert.equal(await mod.capabilities.probe(), 'engine');
	globalThis.fetch = originalFetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	globalThis.fetch = originalFetch;
});

// ------------------------------------------------------------ the decision

test('the overlay shows exactly when the daemon says show the wizard', () => {
	assert.equal(mod.shouldShowFirstRun(null, status({ should_show_wizard: true })), true);
	assert.equal(mod.shouldShowFirstRun(null, status({ should_show_wizard: false })), false);
});

test('the browser adds no dev-mode logic of its own', () => {
	// The engine already suppresses the wizard for a repo checkout, and
	// dev_mode is there to SAY so. A second rule here could disagree with it.
	assert.equal(
		mod.shouldShowFirstRun(null, status({ dev_mode: true, should_show_wizard: false })),
		false
	);
	assert.equal(
		mod.shouldShowFirstRun(null, status({ dev_mode: false, should_show_wizard: true })),
		true
	);
});

test('a refused setup surface never shows the overlay', () => {
	// A legacy boot has no /api/v1/setup at all, so there is nothing to offer.
	assert.equal(mod.shouldShowFirstRun('setup API not offered', status()), false);
});

test('no status means no overlay', () => {
	assert.equal(mod.shouldShowFirstRun(null, null), false);
});

// ------------------------------------------------------------ the resolver

test('resolveFirstRun asks the engine and returns its verdict', async () => {
	const paths = [];
	globalThis.fetch = async (request) => {
		paths.push(new URL(request.url).pathname);
		if (new URL(request.url).pathname === '/api/v1/health') {
			return jsonResponse(engineHealth());
		}
		return jsonResponse(status({ should_show_wizard: true }));
	};
	mod.capabilities._resetForTests();
	assert.equal(await mod.resolveFirstRun(), true);
	assert.ok(paths.includes('/api/v1/setup/status'), `asked ${paths.join(', ')}`);
});

test('resolveFirstRun retries after initial probe failure and still calls setup status', async () => {
	let healthCalls = 0;
	const paths = [];
	globalThis.fetch = async (request) => {
		const path = new URL(request.url).pathname;
		paths.push(path);
		if (path === '/api/v1/health') {
			healthCalls += 1;
			if (healthCalls < 3) throw new TypeError('fetch failed: ECONNREFUSED');
			return jsonResponse(engineHealth());
		}
		if (path === '/api/v1/entitlements') {
			return jsonResponse({ detail: 'not found' }, 404);
		}
		return jsonResponse(status({ should_show_wizard: true }));
	};
	mod.capabilities._resetForTests();
	assert.equal(await mod.resolveFirstRun({ timeoutMs: 5_000 }), true);
	assert.ok(
		paths.includes('/api/v1/setup/status'),
		`setup status must run after probe retries: ${paths.join(', ')}`
	);
	assert.ok(healthCalls >= 3);
});

test('resolveFirstRun is not blocked by a parallel entitlements 404', async () => {
	globalThis.fetch = async (request) => {
		const path = new URL(request.url).pathname;
		if (path === '/api/v1/health') return jsonResponse(engineHealth());
		if (path === '/api/v1/entitlements') return jsonResponse({ detail: 'not found' }, 404);
		if (path === '/api/v1/setup/status') {
			return jsonResponse(status({ should_show_wizard: true }));
		}
		throw new Error(`unexpected fetch ${path}`);
	};
	mod.capabilities._resetForTests();
	assert.equal(await mod.resolveFirstRun(), true);
});

test('resolveFirstRun throws after timeout when the engine never answers', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed: ECONNREFUSED');
	};
	mod.capabilities._resetForTests();
	await assert.rejects(
		() => mod.resolveFirstRun({ timeoutMs: 800 }),
		(err) => err.name === 'FirstRunTimeoutError'
	);
});

test('a populated library resolves to no overlay', async () => {
	globalThis.fetch = async (request) => {
		if (new URL(request.url).pathname === '/api/v1/health') {
			return jsonResponse(engineHealth());
		}
		return jsonResponse(status({ library_empty: false, tracks: 42, should_show_wizard: false }));
	};
	mod.capabilities._resetForTests();
	assert.equal(await mod.resolveFirstRun(), false);
});

test('a failed status probe shows the library, not the overlay', async () => {
	// Never blocks the library: a status call that fails is a reason to show
	// the tracks, not to strand the user behind a dialog.
	globalThis.fetch = async (request) => {
		const path = new URL(request.url).pathname;
		if (path === '/api/v1/health') return jsonResponse(engineHealth());
		if (path === '/api/v1/setup/status') return jsonResponse({ detail: 'boom' }, 500);
		throw new Error(`unexpected ${path}`);
	};
	mod.capabilities._resetForTests();
	assert.equal(await mod.resolveFirstRun(), false);
});

test('resolveFirstRunWithMeta surfaces probe timeout instead of silent false', async () => {
	mod.capabilities._resetForTests();
	globalThis.fetch = async () => jsonResponse({ status: 'ok' }, 503);
	const result = await mod.resolveFirstRunWithMeta({ probeTimeoutMs: 300, probeIntervalMs: 50 });
	assert.equal(result.show, false);
	assert.ok(result.error, 'expected a visible timeout error');
});

test('unknown flavor retries until engine answers then requests setup status', async () => {
	mod.capabilities._resetForTests();
	let healthCalls = 0;
	const paths = [];
	globalThis.fetch = async (request) => {
		const path = new URL(request.url).pathname;
		if (path === '/api/v1/health') {
			healthCalls += 1;
			if (healthCalls < 2) {
				return jsonResponse({ status: 'ok' }, 503);
			}
			return jsonResponse(engineHealth());
		}
		paths.push(path);
		return jsonResponse(status({ should_show_wizard: true }));
	};
	const result = await mod.resolveFirstRunWithMeta();
	assert.equal(result.show, true);
	assert.equal(result.error, null);
	assert.ok(paths.includes('/api/v1/setup/status'));
});

test('legacy flavor never requests setup status', async () => {
	mod.capabilities._resetForTests();
	const paths = [];
	globalThis.fetch = async (request) => {
		const path = new URL(request.url).pathname;
		paths.push(path);
		if (path === '/api/v1/health') {
			return jsonResponse({
				status: 'ok',
				state_db: { path: 'data/state/state.db', tracks: 0, playlists: 0 },
				cloud: { lock_holder: null },
				syncthing: null,
				bind_host: '127.0.0.1',
				version: '0.1.0'
			});
		}
		return jsonResponse(status());
	};
	const result = await mod.resolveFirstRunWithMeta();
	assert.equal(result.show, false);
	assert.equal(result.error, null);
	assert.ok(!paths.includes('/api/v1/setup/status'));
});

// -------------------------------------------------------------- the markup

test('the gate lives in the root layout, not on the library page', () => {
	// One gate. A copy on the library page would be a second decision about
	// the same thing, and it would never fire on /performance -- the route
	// the packaged shell actually lands on.
	const page = read('src/routes/+page.svelte');
	assert.doesNotMatch(page, /resolveFirstRun\(\)/);
	assert.doesNotMatch(page, /FirstRunOverlay/);

	const layout = read('src/routes/+layout.svelte');
	assert.match(layout, /runFirstRunGate\(\)/);
	assert.match(layout, /openSetupOverlay\(\)/);
	assert.match(layout, /<SetupOverlay \/>/);
	// The destination is spelled once, in run-setup.ts.
	assert.match(layout, /SETUP_HOST_ROUTE/);
	assert.doesNotMatch(layout, /goto\(\s*['"]\/performance['"]\s*\)/);
});

test('the overlay dims the app rather than covering it', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /position:\s*fixed/);
	assert.match(overlay, /inset:\s*0/);
	// Translucent on purpose: the point is that the app is visible beneath.
	assert.match(overlay, /background:\s*rgba\(0,\s*0,\s*0,\s*0\.55\)/);
	assert.match(overlay, /z-index/);
});

test('the overlay is a labelled dialog hosting the wizard steps', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /role="dialog"/);
	assert.match(overlay, /aria-modal="true"/);
	assert.match(overlay, /aria-label="First-run setup"/);
	// The steps are REUSED, not rewritten: same store, same step names. The
	// overlay walks visibleSteps(source) rather than the raw list, because the
	// folder branch skips the rekordbox confirm step; both come from the one
	// wizard module, so there is still no second copy of the step names here.
	assert.match(overlay, /from '\$lib\/setup\/wizard\.svelte'/);
	assert.match(overlay, /visibleSteps\(source\)/);
	assert.match(overlay, /setupWizard\.beginImport/);
	assert.match(overlay, /setupWizard\.beginFolderImport/);
});

test('the overlay mounts the assistant sidebar on the agreed contract', () => {
	// The sidebar's internals belong to another lane; { visible } is the
	// whole contract and this component must not reach past it.
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /import AssistantSidebar from '\$lib\/components\/assistant\/AssistantSidebar\.svelte'/);
	assert.match(overlay, /<AssistantSidebar visible=\{true\} \/>/);
});

test('the overlay can be minimised to a chip without closing', () => {
	// A 10,000-track import must not hold the whole screen hostage, and the
	// chip must reopen the SAME wizard rather than restart it.
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /collapseSetupOverlay\(\)/);
	assert.match(overlay, /expandSetupOverlay\(\)/);
	assert.match(overlay, /class="su-chip"/);
	assert.match(overlay, /Importing \{pct\}%/);
});

test('/setup is a door into the overlay, never a 404 and never a second wizard', () => {
	const route = read('src/routes/setup/+page.svelte');
	assert.match(route, /openSetupOverlay\(\)/);
	assert.match(route, /goto\(SETUP_HOST_ROUTE, \{ replaceState: true \}\)/);
	// The wizard markup lives in exactly one place now.
	assert.doesNotMatch(route, /WIZARD_STEPS/);
	assert.doesNotMatch(route, /beginImport/);
});
