/**
 * The first-run gate on the library page: an overlay, not a redirect.
 *
 * A brand new user landing on an empty table with no explanation is the
 * problem; being thrown onto /setup before they have seen the app is the
 * overcorrection. The gate now dims the library and offers one button, so
 * the thing they just installed is visible underneath the ask.
 *
 * The decision is a pure function plus one async resolver, executed here for
 * real. Only the markup facts a node:test harness cannot render are pinned by
 * source shape, the way capability-gating-markup.test.mjs pins its own.
 *
 * Regression lines:
 * - if the page goes back to goto('/setup') then the app auto-navigates away
 *   from itself before the user has seen it
 * - if the gate stops honouring setupRefusal then a legacy boot fires a
 *   request at an endpoint that is guaranteed to 404
 * - if a failed status probe shows the overlay anyway then a transient error
 *   strands the user behind a dialog over a library that loaded fine
 * - if the overlay stops being fixed + full-inset then it no longer dims the
 *   app it is explaining
 * - if the overlay is opaque then the "see what you installed" point is gone
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
		return jsonResponse(status({ should_show_wizard: true }));
	};
	assert.equal(await mod.resolveFirstRun(), true);
	assert.ok(paths.includes('/api/v1/setup/status'), `asked ${paths.join(', ')}`);
});

test('a populated library resolves to no overlay', async () => {
	globalThis.fetch = async () =>
		jsonResponse(status({ library_empty: false, tracks: 42, should_show_wizard: false }));
	assert.equal(await mod.resolveFirstRun(), false);
});

test('a failed status probe shows the library, not the overlay', async () => {
	// Never blocks the library: a status call that fails is a reason to show
	// the tracks, not to strand the user behind a dialog.
	globalThis.fetch = async () => jsonResponse({ detail: 'boom' }, 500);
	assert.equal(await mod.resolveFirstRun(), false);
});

// -------------------------------------------------------------- the markup

test('the library page no longer navigates away to /setup', () => {
	const page = read('src/routes/+page.svelte');
	assert.doesNotMatch(page, /goto\(\s*['"]\/setup['"]\s*\)/);
	assert.match(page, /FirstRunOverlay/);
	assert.match(page, /resolveFirstRun\(\)/);
});

test('the overlay dims the app rather than covering it', () => {
	const overlay = read('src/lib/components/rb/FirstRunOverlay.svelte');
	assert.match(overlay, /position:\s*fixed/);
	assert.match(overlay, /inset:\s*0/);
	// Translucent on purpose: the point is that the app is visible beneath.
	assert.match(overlay, /background:\s*rgba\(0,\s*0,\s*0,\s*0\.45\)/);
	assert.match(overlay, /z-index/);
});

test('the overlay says what it is and offers exactly one way forward', () => {
	const overlay = read('src/lib/components/rb/FirstRunOverlay.svelte');
	assert.match(overlay, /Open DJ/);
	assert.match(overlay, /Import your library to get started/);
	assert.match(overlay, /Run setup/);
	assert.match(overlay, /goto\(\s*['"]\/setup['"]\s*\)/);
});
