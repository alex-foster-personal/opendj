// requirement: ADMIN-03
// Non-markdown touch keeps pull_request CI armed when HEAD would otherwise be docs-only.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const SRC = join(HERE, '../../src');
const API_BASE = 'https://store-build.example.test';

const JOBS_MISSING = 'jobs API not offered by this daemon (no /api/v1/jobs on a legacy boot)';
const EVENTS_MISSING = 'event bus not offered by this daemon (no /api/v1/events on a legacy boot)';
const INERT_PARITY_TODO = 'not implemented - see PARITY-TODO';

const HELPER_PATH = join(SRC, 'routes/admin/entitlements-inspector.ts');
const PAGE_PATH = join(SRC, 'routes/admin/+page.svelte');

function legacyHealth() {
	return {
		status: 'ok',
		state_db: { path: 'data/state/state.db', tracks: 1, playlists: 1, pairings: 0 },
		cloud: { lock_holder: null },
		syncthing: null,
		bind_host: '127.0.0.1',
		version: '0.1.0'
	};
}

function engineHealth() {
	return {
		...legacyHealth(),
		contract_rev: 'sha256:2f6c',
		engine_version: '0.1.0',
		boot_id: 'boot-1'
	};
}

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

function requestUrl(input) {
	return input instanceof Request ? input.url : String(input);
}

let bundle;
let originalFetch;
let originalConsoleError;

function inspectorBundle() {
	return bundleSvelteEntry(`
		export { collectInspectorSnapshot } from './src/routes/admin/entitlements-inspector.ts';
		export { capabilities, jobsRefusal, progressRefusal, eventsRefusal } from '$lib/api/capabilities.svelte';
		export { entitlements, planRefusal } from '$lib/api/entitlements.svelte';
		export { buildFlags, storeBuildRefusal } from '$lib/api/store-build.svelte';
		export { rekordboxWriteback } from '$lib/rb/rekordbox-writeback.svelte';
		export { default as EntitlementsInspectorComponent } from './src/routes/admin/EntitlementsInspector.svelte';
	`);
}

function row(snapshot, id) {
	return snapshot.rows.find((entry) => entry.id === id);
}

before(async () => {
	bundle = await inspectorBundle();
	originalFetch = globalThis.fetch;
	originalConsoleError = console.error;
});

after(() => {
	globalThis.fetch = originalFetch;
	console.error = originalConsoleError;
});

beforeEach(() => {
	bundle.capabilities._resetForTests();
	bundle.entitlements._resetForTests();
	bundle.buildFlags._resetForTests();
	bundle.rekordboxWriteback._resetForTests();
	console.error = () => undefined;
});

function routeFetch(handlers) {
	globalThis.fetch = async (input) => {
		const url = requestUrl(input);
		for (const [suffix, handler] of Object.entries(handlers)) {
			if (url.endsWith(suffix)) {
				return typeof handler === 'function' ? handler() : handler;
			}
		}
		throw new Error(`unexpected fetch: ${url}`);
	};
}

test('engine health: jobs and progress offered', async () => {
	routeFetch({ '/api/v1/health': () => jsonResponse(engineHealth()) });
	await bundle.capabilities.probe();
	const snapshot = bundle.collectInspectorSnapshot();

	assert.equal(row(snapshot, 'jobs')?.refusal, null);
	assert.equal(row(snapshot, 'progressLedger')?.refusal, null);
});

test('legacy health: progress offered, jobs and events refused', async () => {
	routeFetch({ '/api/v1/health': () => jsonResponse(legacyHealth()) });
	await bundle.capabilities.probe();
	const snapshot = bundle.collectInspectorSnapshot();

	assert.equal(row(snapshot, 'progressLedger')?.refusal, null);
	assert.equal(row(snapshot, 'jobs')?.refusal, JOBS_MISSING);
	assert.equal(row(snapshot, 'events')?.refusal, EVENTS_MISSING);
});

test('empty entitlements catalog: no feature rows, no invented plan refusal', async () => {
	routeFetch({
		'/api/v1/health': () => jsonResponse(engineHealth()),
		'/api/v1/entitlements': () =>
			jsonResponse({
				plan: { plan_id: 'free', label: 'Free', note: 'no gate', provider: null },
				features: [],
				refusal: { ui_title: 'not included in your plan - see your account for what is included' }
			})
	});
	await bundle.capabilities.probe();
	await bundle.entitlements.load();
	const snapshot = bundle.collectInspectorSnapshot();

	const featureRows = snapshot.rows.filter((entry) => entry.kind === 'entitlement' && entry.id !== 'plan');
	assert.equal(featureRows.length, 0);
	assert.ok(!snapshot.activeRefusals.some((entry) => entry.kind === 'entitlement'));
});

test('one unentitled feature: refusal equals server ui_title', async () => {
	const uiTitle = 'Premium feature - upgrade required';
	routeFetch({
		'/api/v1/entitlements': () =>
			jsonResponse({
				plan: { plan_id: 'free', label: 'Free', note: 'no gate', provider: null },
				features: [{ feature_id: 'cloudsync', entitled: false, quota: null }],
				refusal: { ui_title: uiTitle }
			})
	});
	await bundle.entitlements.load();
	const snapshot = bundle.collectInspectorSnapshot();

	assert.equal(row(snapshot, 'plan.cloudsync')?.refusal, uiTitle);
	assert.equal(bundle.entitlements.refusalTitle, uiTitle);
});

test('store-build refusal on flag row; locally-disabled flag has null refusal', async () => {
	const storeTitle = 'Not available in the App Store build';
	routeFetch({
		'/api/v1/flags': () =>
			jsonResponse({
				build_profile: 'full',
				sandboxed: false,
				flags: [
					{
						flag_id: 'usb.export',
						enabled: false,
						refusal: { ui_title: storeTitle }
					},
					{
						flag_id: 'local_stems.executor',
						enabled: false,
						refusal: null
					}
				]
			})
	});
	await bundle.buildFlags.load();
	const snapshot = bundle.collectInspectorSnapshot();

	assert.equal(row(snapshot, 'usb.export')?.refusal, storeTitle);
	assert.equal(row(snapshot, 'usb.export')?.value, 'off');
	assert.equal(row(snapshot, 'local_stems.executor')?.refusal, null);
	assert.equal(row(snapshot, 'local_stems.executor')?.value, 'off');
	assert.equal(bundle.storeBuildRefusal('usb.export'), storeTitle);
	assert.equal(bundle.storeBuildRefusal('local_stems.executor'), null);
});

test('canonical inert row is present but excluded from activeRefusals', () => {
	const snapshot = bundle.collectInspectorSnapshot();
	const inert = row(snapshot, 'inert.parity-todo');
	assert.ok(inert);
	assert.equal(inert.refusal, INERT_PARITY_TODO);
	assert.ok(!snapshot.activeRefusals.some((entry) => entry.id === 'inert.parity-todo'));
});

test('progressRefusal on snapshot matches progressRefusal() after probe', async () => {
	routeFetch({ '/api/v1/health': () => jsonResponse(engineHealth()) });
	await bundle.capabilities.probe();
	const snapshot = bundle.collectInspectorSnapshot();
	assert.equal(row(snapshot, 'progressLedger')?.refusal, bundle.progressRefusal());
});

test('SSR: engine capabilities, empty entitlements, loaded flags', async () => {
	routeFetch({
		'/api/v1/health': () => jsonResponse(engineHealth()),
		'/api/v1/entitlements': () =>
			jsonResponse({
				plan: { plan_id: 'free', label: 'Free', note: 'no gate', provider: null },
				features: [],
				refusal: { ui_title: 'not included in your plan - see your account for what is included' }
			}),
		'/api/v1/flags': () =>
			jsonResponse({
				build_profile: 'full',
				sandboxed: false,
				flags: [{ flag_id: 'usb.export', enabled: true, refusal: null }]
			}),
		'/api/v1/rekordbox/writeback-gate': () =>
			jsonResponse({ enabled: true, ui_title: 'sync to rekordbox disabled - one-way import only' })
	});
	await bundle.capabilities.probe();
	await bundle.entitlements.load();
	await bundle.buildFlags.load();
	await bundle.rekordboxWriteback.probe();

	const html = await renderToHtml(bundle.EntitlementsInspectorComponent);
	assert.ok(html.includes('data-testid="entitlements-inspector"'));
	assert.ok(html.includes('Entitlements inspector'));
	assert.ok(html.includes('engine'));
	assert.ok(html.includes('progressLedger'));
	assert.ok(html.includes('No session-level refusals right now.'));
	assert.ok(!html.includes('Every capability openDJ ships is available'));
});

test('legacy daemon 404: fail-open without console.error flood', async () => {
	const consoleErrors = [];
	console.error = (...args) => {
		consoleErrors.push(args);
	};
	routeFetch({
		'/api/v1/entitlements': () => new Response(null, { status: 404 })
	});
	await bundle.entitlements.load();

	assert.equal(consoleErrors.length, 0);
	assert.equal(bundle.entitlements.loaded, false);
	assert.ok(bundle.entitlements.error !== null);
	assert.equal(bundle.entitlements.plan, null);
	assert.equal(bundle.entitlements.features.length, 0);
	assert.equal(bundle.planRefusal('cloudsync'), null);
});

test('non-404 entitlement failure still reaches console.error', async () => {
	const consoleErrors = [];
	console.error = (...args) => {
		consoleErrors.push(args);
	};
	routeFetch({
		'/api/v1/entitlements': () => jsonResponse({ detail: 'nope' }, 500)
	});
	await bundle.entitlements.load();

	assert.equal(consoleErrors.length, 1);
	assert.match(String(consoleErrors[0][0]), /\[entitlements\] load failed/);
	assert.equal(bundle.entitlements.loaded, false);
	assert.ok(bundle.entitlements.error !== null);
});

test('SSR: entitlements error still lists flags', async () => {
	routeFetch({
		'/api/v1/health': () => jsonResponse(engineHealth()),
		'/api/v1/entitlements': () => jsonResponse({ detail: 'nope' }, 500),
		'/api/v1/flags': () =>
			jsonResponse({
				build_profile: 'full',
				sandboxed: false,
				flags: [{ flag_id: 'usb.export', enabled: true, refusal: null }]
			})
	});
	await bundle.capabilities.probe();
	await bundle.entitlements.load();
	await bundle.buildFlags.load();

	const html = await renderToHtml(bundle.EntitlementsInspectorComponent);
	assert.ok(html.includes('usb.export'));
	assert.ok(html.length > 0);
	assert.ok(bundle.entitlements.error !== null);
});

test('+page.svelte mounts EntitlementsInspector before lyrics-generator', () => {
	const source = readFileSync(PAGE_PATH, 'utf8');
	assert.match(source, /import EntitlementsInspector from '\.\/EntitlementsInspector\.svelte'/);
	const inspectorPos = source.indexOf('<EntitlementsInspector />');
	const lyricsPos = source.indexOf('id="lyrics-generator"');
	assert.ok(inspectorPos >= 0 && lyricsPos > inspectorPos);
});

test('helper calls every refusal function by name', () => {
	const source = readFileSync(HELPER_PATH, 'utf8');
	for (const fn of [
		'jobsRefusal',
		'progressRefusal',
		'eventsRefusal',
		'planRefusal',
		'storeBuildRefusal',
		'finalSetupRefusal',
		'rekordboxWritebackRefusal'
	]) {
		assert.match(source, new RegExp(`\\b${fn}\\(`));
	}
	assert.ok(source.includes(`'${INERT_PARITY_TODO}'`));
});
