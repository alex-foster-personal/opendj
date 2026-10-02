/**
 * The ways BACK into setup, once the first-run overlay has been answered.
 *
 * The overlay is a one-shot: the engine records the dismissal in the data dir
 * and never offers it again. That was the whole story, so a tester who had
 * skipped setup, or who had imported once and wanted to import again, had NO
 * route to /setup from inside the running app. This file pins the three doors
 * that now exist -- the Cmd+, settings overlay, the /admin Setup tab and the
 * /settings page -- and the one shared module all three call.
 *
 * The decision logic is executed for real against the REAL generated client,
 * with globalThis.fetch as the only seam, exactly as setup-wizard.test.mjs
 * does. Only the markup facts a node:test harness cannot render are pinned by
 * source shape.
 *
 * Regression lines:
 * - if runSetup reads the refusal before awaiting the capability probe then a
 *   click in the first tick after load is answered with "daemon not
 *   identified yet" on a perfectly healthy engine
 * - if runSetup navigates after a failed re-arm then the wizard is on screen
 *   while the engine still holds the dismissal
 * - if a legacy boot's Run setup issues any request then it fires at an
 *   endpoint guaranteed to 404
 * - if runSetupBlocked disables on an UNKNOWN flavor then an unfinished probe
 *   reads as a refusal, which is a hidden default
 * - if the Cmd+, overlay loses its Run setup action then the only way back
 *   into setup is a URL somebody has to already know
 * - if the Run setup action moves inside .so-body then it is invisible until
 *   the user types a search term first
 * - if the admin tab strip loses its Setup tab then the operator panel has no
 *   route into setup
 * - if any surface spells '/setup' itself instead of using SETUP_ROUTE then
 *   the doors can drift apart
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { engineHealth, jsonResponse } from './setup-fixtures.mjs';

const API_BASE = 'https://entry-points.example.test';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

/** SetupStatusOut, shaped like the real one. Returned by the dismiss write. */
function status(overrides = {}) {
	return {
		library_empty: false,
		tracks: 42,
		playlists: 3,
		state_db: { path: '/data/state/state.db', exists: true, size_bytes: 4096, modified_at: null },
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

/** The LEGACY daemon: same health route, none of the engine handshake fields.
 * Its presence is what makes the refusal test a real one. */
function legacyHealth() {
	const { contract_rev, engine_version, boot_id, ...rest } = engineHealth();
	return rest;
}

/**
 * A fresh module graph. `capabilities.probe()` memoizes on success, so each
 * daemon-flavor scenario needs its own instance or the first probe decides
 * every later test.
 */
async function loadFresh() {
	return loadTypeScriptModule('tests/unit/fixtures/run-setup-entry.ts', {
		viteApiBase: API_BASE
	});
}

let originalFetch;

before(() => {
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	globalThis.fetch = originalFetch;
});

// ------------------------------------------------------- the shared module

test('runSetup re-arms the wizard and navigates, in that order', async () => {
	const mod = await loadFresh();
	const calls = [];
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		calls.push(`${request.method} ${url.pathname}`);
		if (url.pathname === '/api/v1/health') return jsonResponse(engineHealth());
		if (url.pathname === '/api/v1/setup/dismiss') return jsonResponse(status());
		throw new Error(`unexpected request: ${request.method} ${url.pathname}`);
	};
	const navigated = [];
	assert.equal(await mod.runSetup((path) => navigated.push(path)), null);
	assert.deepEqual(navigated, [mod.SETUP_ROUTE]);
	assert.equal(mod.SETUP_ROUTE, '/setup');
	// The dismissal is cleared BEFORE the navigation, so the page never loads
	// while the engine still believes setup was declined.
	assert.deepEqual(calls, ['GET /api/v1/health', 'POST /api/v1/setup/dismiss']);
});

test('runSetup awaits the probe, so a click in the first tick still works', async () => {
	// The race that produced "daemon not identified yet" on a healthy engine:
	// nothing has probed yet when the button is pressed. If runSetup read the
	// refusal synchronously this would refuse instead of navigating.
	const mod = await loadFresh();
	assert.equal(mod.capabilities.flavor, 'unknown');
	globalThis.fetch = async (request) => {
		const { pathname } = new URL(request.url);
		if (pathname === '/api/v1/health') return jsonResponse(engineHealth());
		return jsonResponse(status());
	};
	const navigated = [];
	assert.equal(await mod.runSetup((path) => navigated.push(path)), null);
	assert.deepEqual(navigated, ['/setup']);
});

test('a legacy boot refuses, navigates nowhere and writes nothing', async () => {
	const mod = await loadFresh();
	const calls = [];
	globalThis.fetch = async (request) => {
		const { pathname } = new URL(request.url);
		calls.push(pathname);
		if (pathname === '/api/v1/health') return jsonResponse(legacyHealth());
		throw new Error(`a legacy boot must not be asked for ${pathname}`);
	};
	const navigated = [];
	const failure = await mod.runSetup((path) => navigated.push(path));
	assert.match(failure, /setup API not offered/);
	assert.deepEqual(navigated, []);
	assert.deepEqual(calls, ['/api/v1/health']);
});

test('a failed re-arm does not navigate', async () => {
	// Landing on the wizard while the dismissal is still set would be two
	// truths about one decision.
	const mod = await loadFresh();
	globalThis.fetch = async (request) => {
		const { pathname } = new URL(request.url);
		if (pathname === '/api/v1/health') return jsonResponse(engineHealth());
		return jsonResponse({ detail: { code: 'boom', message: 'dismiss write failed' } }, 500);
	};
	const navigated = [];
	const failure = await mod.runSetup((path) => navigated.push(path));
	assert.match(failure, /dismiss write failed|Something went wrong/);
	assert.match(mod.setupWizard.errorDiagnostic ?? '', /dismiss write failed/);
	assert.deepEqual(navigated, []);
});

test('an unknown flavor does not disable the control; a legacy one does', async () => {
	const mod = await loadFresh();
	// Unknown is "not yet", not "no". Disabling here would turn an unfinished
	// health GET into a permission answer.
	assert.equal(mod.capabilities.flavor, 'unknown');
	assert.equal(mod.runSetupBlocked(), null);

	globalThis.fetch = async () => jsonResponse(legacyHealth());
	assert.equal(await mod.capabilities.probe(), 'legacy');
	assert.match(mod.runSetupBlocked(), /not available in this version/);
});

test('an engine never blocks the control', async () => {
	const mod = await loadFresh();
	globalThis.fetch = async () => jsonResponse(engineHealth());
	assert.equal(await mod.capabilities.probe(), 'engine');
	assert.equal(mod.runSetupBlocked(), null);
});

test('the wizard load path awaits the probe too', async () => {
	// Same race, the other door: /setup mounts and calls load() in the same
	// tick the root layout fires its probe. Reading the refusal first pinned
	// "daemon not identified yet" permanently, because nothing re-runs load().
	const mod = await loadFresh();
	globalThis.fetch = async (request) => {
		const { pathname } = new URL(request.url);
		if (pathname === '/api/v1/health') return jsonResponse(engineHealth());
		return jsonResponse(status({ tracks: 7 }));
	};
	await mod.setupWizard.load();
	assert.equal(mod.setupWizard.error, null);
	assert.equal(mod.setupWizard.status?.tracks, 7);
});

// -------------------------------------------------------------- the markup

test('the Cmd+, overlay offers Run setup, outside the collapsible body', () => {
	const overlay = read('src/lib/components/settings/SettingsOverlay.svelte');
	assert.match(overlay, /from '\$lib\/setup\/run-setup'/);
	assert.match(overlay, /runSetup\(goto\)/);
	assert.match(overlay, /RUN_SETUP_LABEL/);
	// .so-body is display:none until the panel expands, so an action rendered
	// inside it is unreachable until the user types a search term.
	const actionsAt = overlay.indexOf('<div class="so-actions">');
	const bodyAt = overlay.indexOf('<div class="so-body"');
	assert.ok(actionsAt > 0, 'the overlay has an actions bar');
	assert.ok(actionsAt < bodyAt, 'the actions bar is outside (and above) .so-body');
});

test('Cmd+, is a real accelerator, not a label', () => {
	// The sidebar button says "Settings (Cmd+,)". The chord has to exist.
	const hotkeys = read('src/lib/settings/hotkeys.ts');
	assert.match(hotkeys, /metaKey \|\| e\.ctrlKey/);
	assert.match(hotkeys, /SETTINGS_CHORD_KEY/);
	assert.match(hotkeys, /SETTINGS_CHORD_CODE/);
	assert.match(
		hotkeys,
		/e\.key === SETTINGS_CHORD_KEY \|\| e\.code === SETTINGS_CHORD_CODE/
	);
	assert.match(hotkeys, /addEventListener\('keydown'/);
	// Installed from the ROOT layout, so it fires on /performance and inside
	// the packaged shell's webview, not only on the app-shell routes.
	const layout = read('src/routes/+layout.svelte');
	assert.match(layout, /installSettingsHotkeys\(\)/);
});

test('the admin panel has a tab strip and Setup is one of the tabs', () => {
	const admin = read('src/routes/admin/+page.svelte');
	assert.match(admin, /role="tablist"/);
	const tabs = [...admin.matchAll(/role="tab"/g)];
	assert.ok(tabs.length >= 2, `expected a strip, found ${tabs.length} tab(s)`);
	assert.match(admin, /KPI ledger/);
	assert.match(admin, /Diagnostics/);
	assert.match(admin, /Playground/);
	assert.match(admin, /tab=playground|selectTab\('playground'\)/);
	assert.match(admin, /tab=diagnostics|adminTabFromUrl|from '\.\/admin-tab'/);
	assert.match(admin, />\s*\{setupBusy \? 'Opening setup\.\.\.' : 'Setup'\}\s*</);
	// Through the shared module, so the tab behaves like the other two doors.
	assert.match(admin, /from '\$lib\/setup\/run-setup'/);
	assert.match(admin, /runSetup\(goto\)/);
});

test('every entry point goes through the shared module, none reimplements it', () => {
	for (const relative of [
		'src/lib/components/settings/SettingsOverlay.svelte',
		'src/routes/admin/+page.svelte',
		'src/routes/settings/+page.svelte'
	]) {
		const source = read(relative);
		assert.match(source, /from '\$lib\/setup\/run-setup'/, relative);
		// The destination is spelled once, in run-setup.ts. A literal here is
		// how three doors start disagreeing.
		assert.doesNotMatch(source, /goto\(\s*['"]\/setup['"]\s*\)/, relative);
		// Re-arming is the shared module's job; a local copy is the drift.
		assert.doesNotMatch(source, /setupWizard\.reopen\(\)/, relative);
	}
});
