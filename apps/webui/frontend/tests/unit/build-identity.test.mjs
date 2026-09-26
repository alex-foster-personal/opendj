/**
 * The build stamp must never be able to look fine when it is not.
 *
 * Every test here is a variation on one failure: a tester used a stale, dirty
 * build for an hour because nothing on screen distinguished it from a good
 * one. So the assertions are about what is REFUSED -- no fabricated sha, no
 * silent blank, no "aligned" verdict pulled out of an unknown.
 *
 * The chip also answers WHERE this app is. The packaged engine binds an
 * EPHEMERAL port, so the address differs every launch; a tester who wanted to
 * open the app in a browser guessed the dev port and got nothing.
 *
 * Regression lines:
 * - if engineBaseUrl invents an address when it cannot derive one then the
 *   reader is sent to a port nothing is listening on
 * - if a configured VITE_API_BASE stops beating the page origin then a dev
 *   build names the Vite server instead of the daemon it talks to
 * - if the chip goes back to position:fixed then it floats on top of the
 *   sidebar and the browser panel's connectivity dots again
 * - if the foldout stops opening upward then it resizes the tray row it
 *   lives in
 * - if the app shell loses its bottom tray then the chip has nowhere to sit
 * - if the browser panel's bottom bar stops mounting the chip then
 *   /performance shows no build identity at all
 * - if the address loses its selectable text or its copy control then
 *   finding the app's URL is a transcription exercise again
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ENGINE_OK = {
	source: 'payload',
	engine_version: '0.1.0',
	git_sha: '81eebe75',
	git_sha_full: '81eebe75c0ffee0000000000000000000000abcd',
	git_branch: 'af--engine-sidecar',
	git_dirty: false,
	built_at_utc: '2026-08-19T12:31:39Z',
	built_at_kind: 'payload-build',
	lane_label: 'B',
	product_name: 'Open DJ (B)',
	bundle_identifier: 'com.opendj.desktop.lane-b',
	app_version: '0.1.0',
	manifest_path: '/Applications/Open DJ (B).app/Contents/Resources/payload/manifest.json'
};

const SHELL_OK = {
	stamped: true,
	app_version: '0.1.0',
	git_sha: '81eebe75',
	git_sha_full: '81eebe75c0ffee0000000000000000000000abcd',
	git_branch: 'af--engine-sidecar',
	git_dirty: false,
	built_at_utc: '2026-08-19T12:30:02Z',
	lane_label: 'B'
};

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/build-identity.ts');
});

function jsonFetch(status, body) {
	return async () =>
		new Response(JSON.stringify(body), {
			status,
			headers: { 'content-type': 'application/json' }
		});
}

// ----- shell side ---------------------------------------------------------
test('a browser tab reports absent, not a fault and not a value', () => {
	const state = mod.readShellBuild({});
	assert.equal(state.kind, 'absent');
	assert.match(state.reason, /OPENDJ_SHELL_BUILD/);
});

test('a shell compiled without its git stamp is a FAULT, never a blank', () => {
	const state = mod.readShellBuild({
		OPENDJ_SHELL_BUILD: { stamped: false, git_sha: null, app_version: '0.1.0' }
	});
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /without its git stamp/);
});

test('a stamped shell yields its identity verbatim', () => {
	const state = mod.readShellBuild({ OPENDJ_SHELL_BUILD: SHELL_OK });
	assert.equal(state.kind, 'ok');
	assert.equal(state.value.git_sha, '81eebe75');
	assert.equal(state.value.lane_label, 'B');
	assert.equal(state.value.release_channel, null);
	assert.equal(state.value.evidence_written_at_utc, null);
});

test('a stamped shell yields channel and evidence timestamp', () => {
	const state = mod.readShellBuild({
		OPENDJ_SHELL_BUILD: {
			...SHELL_OK,
			release_channel: 'stable',
			evidence_written_at_utc: '2026-09-12T12:00:00Z'
		}
	});
	assert.equal(state.kind, 'ok');
	assert.equal(state.value.release_channel, 'stable');
	assert.equal(state.value.evidence_written_at_utc, '2026-09-12T12:00:00Z');
});

// ----- engine side --------------------------------------------------------
test('a 200 yields the engine identity', async () => {
	const state = await mod.fetchEngineBuild(jsonFetch(200, ENGINE_OK), '');
	assert.equal(state.kind, 'ok');
	assert.equal(state.value.source, 'payload');
	assert.equal(state.value.git_sha_full, ENGINE_OK.git_sha_full);
});

test('a 503 renders the engine reason, never an empty readout', async () => {
	const state = await mod.fetchEngineBuild(
		jsonFetch(503, { error: 'build_identity_unavailable', message: 'no manifest at /x' }),
		''
	);
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /no manifest at \/x/);
});

test('a daemon predating the route says so in those words', async () => {
	const state = await mod.fetchEngineBuild(jsonFetch(404, {}), '');
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /predates the build stamp/);
});

test('a dead socket is a fault carrying the transport error', async () => {
	const state = await mod.fetchEngineBuild(async () => {
		throw new TypeError('fetch failed');
	}, '');
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /unreachable/);
	assert.match(state.reason, /fetch failed/);
});

test('a 200 without the identity fields is refused rather than half-rendered', async () => {
	const state = await mod.fetchEngineBuild(jsonFetch(200, { source: 'repo' }), '');
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /without git_sha and built_at_utc/);
});

test('the request goes to the shared API base', async () => {
	const seen = [];
	await mod.fetchEngineBuild(async (url) => {
		seen.push(String(url));
		return new Response('{}', { status: 200 });
	}, 'https://engine.example.test');
	assert.deepEqual(seen, ['https://engine.example.test/api/v1/build-info']);
});

// ----- rendering ----------------------------------------------------------
test('one instant renders as BOTH local and UTC', () => {
	const stamp = mod.formatStamp('2026-08-19T12:31:39Z', 'Australia/Sydney');
	assert.equal(stamp.utc, '2026-08-19 12:31Z');
	// Sydney is UTC+10 in August, so local must be the same instant, not the
	// same clock reading -- otherwise the two halves are redundant.
	assert.match(stamp.local, /19 Aug 2026/);
	assert.match(stamp.local, /22:31/);
});

test('an unparseable timestamp renders nothing rather than "Invalid Date"', () => {
	assert.equal(mod.formatStamp('not-a-date'), null);
	assert.equal(mod.formatStamp(null), null);
	assert.equal(mod.formatStamp(''), null);
});

// REQ: INSTALL-18
test('build age reads in whole days and hours, never minutes', () => {
	const now = new Date('2026-09-14T06:30:00Z');
	assert.equal(mod.formatAge('2026-09-13T01:00:00Z', now), '1d 5h');
	assert.equal(mod.formatAge('2026-09-12T06:30:00Z', now), '2d 0h');
	assert.equal(mod.formatAge('2026-09-14T00:31:00Z', now), '5h');
	assert.equal(mod.formatAge('2026-09-14T06:00:00Z', now), '<1h');
});

// REQ: INSTALL-18
test('a build stamped in the future or unparseable has no age, never a negative one', () => {
	const now = new Date('2026-09-14T06:30:00Z');
	assert.equal(mod.formatAge('2026-09-14T07:30:00Z', now), null);
	assert.equal(mod.formatAge('not-a-date', now), null);
	assert.equal(mod.formatAge(null, now), null);
});

test('the dirty marker is part of the one-line label', () => {
	const clean = mod.shortLabel({ kind: 'ok', value: ENGINE_OK });
	const dirty = mod.shortLabel({ kind: 'ok', value: { ...ENGINE_OK, git_dirty: true } });
	assert.equal(clean, '81eebe75');
	assert.equal(dirty, '81eebe75 DIRTY');
});

test('a faulted side never renders a plausible-looking sha', () => {
	assert.equal(mod.shortLabel({ kind: 'fault', reason: 'anything' }), mod.UNKNOWN);
	assert.equal(mod.shortLabel({ kind: 'absent', reason: 'anything' }), mod.UNKNOWN);
});

// ----- drift --------------------------------------------------------------
test('same commit on both sides is aligned', () => {
	const drift = mod.describeDrift(
		{ kind: 'ok', value: SHELL_OK },
		{ kind: 'ok', value: ENGINE_OK }
	);
	assert.equal(drift, 'aligned');
});

test('a shell pointed at a different engine build reports drift', () => {
	const drift = mod.describeDrift(
		{ kind: 'ok', value: SHELL_OK },
		{ kind: 'ok', value: { ...ENGINE_OK, git_sha_full: 'deadbeef'.repeat(5) } }
	);
	assert.equal(drift, 'drifted');
});

test('an unknown side is never reported as aligned', () => {
	assert.equal(
		mod.describeDrift({ kind: 'absent', reason: 'browser' }, { kind: 'ok', value: ENGINE_OK }),
		'unknown'
	);
	assert.equal(
		mod.describeDrift({ kind: 'ok', value: SHELL_OK }, { kind: 'loading' }),
		'unknown'
	);
});

// ----- hover explanation (house rule: every readout explains itself) -------
test('the hover text names the runtime source of each half', () => {
	const engine = mod.explainSide('engine', { kind: 'ok', value: ENGINE_OK });
	assert.match(engine, /GET \/api\/v1\/build-info/);
	assert.match(engine, /manifest\.json/);
	const shell = mod.explainSide('shell', { kind: 'ok', value: SHELL_OK });
	assert.match(shell, /globalThis\.OPENDJ_SHELL_BUILD/);
});

test('a dirty build says in the hover text what dirty means', () => {
	const text = mod.explainSide('engine', {
		kind: 'ok',
		value: { ...ENGINE_OK, git_dirty: true }
	});
	assert.match(text, /uncommitted changes/);
});

test('a faulted side explains itself instead of going quiet', () => {
	const text = mod.explainSide('engine', { kind: 'fault', reason: 'engine unreachable' });
	assert.match(text, /No identity available: engine unreachable/);
});

// ----- the layout contract ------------------------------------------------
test('the component is mounted exactly once from the root layout', async () => {
	const { readFileSync } = await import('node:fs');
	const layout = readFileSync(
		new URL('../../src/routes/+layout.svelte', import.meta.url),
		'utf8'
	);
	// One import, one mount, no restructuring: the root layout is a hotspot
	// file that several builders touch in the same wave.
	const imports = layout.match(/import BuildIdentity from '\$lib\/components\/rb\/BuildIdentity\.svelte';/g);
	const mounts = layout.match(/<BuildIdentity \/>/g);
	assert.equal(imports?.length, 1);
	assert.equal(mounts?.length, 1);
});

test('the component carries no build-time literal of its own', async () => {
	const { readFileSync } = await import('node:fs');
	const source = readFileSync(
		new URL('../../src/lib/components/rb/BuildIdentity.svelte', import.meta.url),
		'utf8'
	);
	// A sha or an ISO timestamp written into this file would be a lie the
	// moment the next commit lands, which is the whole bug.
	assert.equal(/\b[0-9a-f]{7,40}\b/.test(source.replace(/#[0-9a-f]{3,8}\b/g, '')), false);
	assert.equal(/\d{4}-\d{2}-\d{2}T/.test(source), false);
	assert.match(source, /release_channel/);
	assert.match(source, /evidence_written_at_utc/);
	assert.match(source, /evidenceStamp/);
});

// REQ: INSTALL-18
test('the compact built-at reads as age ago and ticks once a minute', async () => {
	const { readFileSync } = await import('node:fs');
	const source = readFileSync(
		new URL('../../src/lib/components/rb/BuildIdentity.svelte', import.meta.url),
		'utf8'
	);
	assert.match(source, /setInterval\(\(\) => \(now = new Date\(\)\), 60_000\)/);
	assert.match(source, /evidenceAge !== null \? `\$\{evidenceAge\} ago` : evidenceStamp\.local/);
	assert.match(source, /engineAge !== null \? `\$\{engineAge\} ago` : engineStamp\.local/);
});

// ----- where this app is --------------------------------------------------
test('an engine-served page reports its own origin as the engine', () => {
	const state = mod.engineBaseUrl({ origin: 'http://127.0.0.1:56146' }, '');
	assert.equal(state.kind, 'ok');
	assert.equal(state.url, 'http://127.0.0.1:56146');
	assert.equal(state.source, 'served');
});

test('a trailing slash is trimmed, so the address is pasteable as-is', () => {
	const state = mod.engineBaseUrl({ origin: 'http://127.0.0.1:56146/' }, '');
	assert.equal(state.url, 'http://127.0.0.1:56146');
});

test('a configured API base beats the page origin', () => {
	// The dev flow: this page came from Vite, the daemon is somewhere else.
	// Naming the page's own origin here would send the reader to the wrong
	// process entirely.
	const state = mod.engineBaseUrl(
		{ origin: 'http://127.0.0.1:9411', href: 'http://127.0.0.1:9411/performance' },
		'http://127.0.0.1:8699'
	);
	assert.equal(state.kind, 'ok');
	assert.equal(state.url, 'http://127.0.0.1:8699');
	assert.equal(state.source, 'configured');
});

test('a relative API base is resolved against the document, not guessed', () => {
	const state = mod.engineBaseUrl(
		{ origin: 'http://127.0.0.1:9411', href: 'http://127.0.0.1:9411/performance' },
		'/daemon'
	);
	assert.equal(state.kind, 'ok');
	assert.equal(state.url, 'http://127.0.0.1:9411/daemon');
	assert.equal(state.source, 'configured');
});

test('no origin and no configured base is a fault, never a plausible address', () => {
	const state = mod.engineBaseUrl(null, '');
	assert.equal(state.kind, 'fault');
	// The exact failure this readout exists to prevent: a made-up port.
	assert.doesNotMatch(state.reason, /127\.0\.0\.1:\d+/);
	assert.doesNotMatch(state.reason, /localhost:\d+/);
});

test('an opaque origin is a fault rather than the string "null"', () => {
	const state = mod.engineBaseUrl({ origin: 'null' }, '');
	assert.equal(state.kind, 'fault');
});

test('a relative base with no document to resolve it against is a fault', () => {
	const state = mod.engineBaseUrl({ origin: 'http://127.0.0.1:9411' }, '/daemon');
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /relative/);
});

test('the address explains where it came from, and a fault says why not', () => {
	const served = mod.explainEngineUrl({
		kind: 'ok',
		url: 'http://127.0.0.1:56146',
		source: 'served'
	});
	assert.match(served, /served BY the engine/);
	assert.match(served, /http:\/\/127\.0\.0\.1:56146/);

	const configured = mod.explainEngineUrl({
		kind: 'ok',
		url: 'http://127.0.0.1:8699',
		source: 'configured'
	});
	assert.match(configured, /VITE_API_BASE/);

	const fault = mod.explainEngineUrl({ kind: 'fault', reason: 'no origin' });
	assert.match(fault, /cannot be determined: no origin/);
});

// ----- the tray contract --------------------------------------------------
// Markup facts a node:test harness cannot render, pinned by source shape --
// the same way the layout-mount test above already does.
test('the chip is a tray citizen, not a floating overlay', async () => {
	const { readFileSync } = await import('node:fs');
	const source = readFileSync(
		new URL('../../src/lib/components/rb/BuildIdentity.svelte', import.meta.url),
		'utf8'
	);
	// position:fixed is what made it sit ON TOP of the sidebar and the
	// connectivity dots. margin-left:auto is what puts it at the RIGHT end of
	// whichever tray mounts it.
	// The DECLARATION, not the prose: the header comment explains the move and
	// necessarily names the thing it moved away from.
	assert.doesNotMatch(source, /position:\s*fixed;/);
	assert.match(source, /position:\s*relative;/);
	assert.match(source, /margin-left:\s*auto/);
	// The foldout opens upward from the tray, so the 18px row never resizes.
	assert.match(source, /bottom:\s*100%/);
});

test('the foldout states the address, selectable, with a copy control', async () => {
	const { readFileSync } = await import('node:fs');
	const source = readFileSync(
		new URL('../../src/lib/components/rb/BuildIdentity.svelte', import.meta.url),
		'utf8'
	);
	assert.match(source, /engineBaseUrl\(\)/);
	assert.match(source, /explainEngineUrl/);
	assert.match(source, /user-select:\s*all/);
	assert.match(source, /class="copy"/);
	assert.match(source, /clipboard\.writeText/);
	// A copy that silently did nothing is worse than no copy button.
	assert.match(source, /copy refused/);
});

test('the version block names itself and distinguishes Chrome from the packaged app', async () => {
	const { readFileSync } = await import('node:fs');
	const source = readFileSync(
		new URL('../../src/lib/components/rb/BuildIdentity.svelte', import.meta.url),
		'utf8'
	);
	assert.match(source, /app versioning:/);
	assert.match(source, /browser identity/);
	assert.match(source, /Chrome dev loop/);
	assert.match(source, /DMG app version/);
	assert.match(source, /shell\.value\.app_version/);
	// Existing drift detection remains the authority when shell and engine differ.
	assert.match(source, /drift === 'drifted'/);
});

test('the app shell mounts the chip inside a bottom tray', async () => {
	const { readFileSync } = await import('node:fs');
	const layout = readFileSync(
		new URL('../../src/routes/+layout.svelte', import.meta.url),
		'utf8'
	);
	assert.match(layout, /class="app-tray"/);
	// The tray wraps the chip; a mount outside it would be the old floating
	// placement wearing a new class name.
	assert.match(layout, /<footer class="app-tray"[^>]*>\s*<BuildIdentity \/>\s*<\/footer>/);
	// Full width of the shell grid, so "right" means the window's right edge.
	assert.match(layout, /grid-column:\s*1\s*\/\s*-1/);
});

test('the update badge stays wired for packaged installs while hiding in dev', async () => {
	const { readFileSync } = await import('node:fs');
	const source = readFileSync(
		new URL('../../src/lib/components/rb/BuildIdentity.svelte', import.meta.url),
		'utf8'
	);
	assert.match(source, /isUpdaterExpected\(/);
	assert.match(source, /summarizeUpdate\(update,\s*\{\s*updaterExpected\s*\}\)/);
	assert.match(source, /class="update-badge"/);
	assert.match(source, /\{#if updateSummary !== null && updateSummary\.prominent\}/);
});

test('the performance route mounts the chip in the browser bottom bar', async () => {
	const { readFileSync } = await import('node:fs');
	const panel = readFileSync(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url),
		'utf8'
	);
	assert.match(panel, /import BuildIdentity from '\.\/BuildIdentity\.svelte';/);
	// Inside the bottom bar and before the resize grip, which is the far
	// right-hand corner of that tray.
	const bar = panel.slice(panel.indexOf('<div class="bottom-bar">'));
	const mount = bar.indexOf('<BuildIdentity />');
	const grip = bar.indexOf('<span class="grip"');
	assert.ok(mount > 0, 'the bottom bar mounts the build identity');
	assert.ok(mount < grip, 'the chip sits before the grip in the tray');
});
