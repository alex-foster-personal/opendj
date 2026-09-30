/**
 * The update channel must never be able to look reassuring when it is not.
 *
 * The failure this guards is narrower and nastier than a broken button: a
 * check that cannot reach its endpoint and renders nothing is read by a human
 * as "I am up to date". So every assertion below is about what is REFUSED --
 * no silent no-op, no blank badge, no install offered where no installer
 * exists.
 *
 * Regression lines:
 * - if an unreachable engine renders as up-to-date then an outage becomes a
 *   false reassurance
 * - if a 502 from the engine is treated as a transport error then the named
 *   channel fault it carries is thrown away
 * - if a status ever renders as an empty badge then a real state is invisible
 * - if a browser tab reports it can install then the install button is dead
 *   on click instead of absent
 * - if applyUpdate resolves outside a Tauri shell then a failed update looks
 *   like a successful one
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ANSWERED_UP_TO_DATE = {
	status: 'up-to-date',
	endpoint: 'https://example.invalid/latest.json',
	platform_key: 'darwin-aarch64',
	current_version: '0.1.0',
	available_version: '0.1.0',
	current_git_sha: '81eebe75',
	current_built_at_utc: '2026-08-31T12:00:00Z',
	published_at: '2026-08-30T00:00:00Z',
	notes: 'built from 81eebe75',
	same_version_different_build: false,
	detail: null,
	applies_via: "the desktop shell's Tauri updater; this endpoint only reports"
};

const AVAILABLE = {
	...ANSWERED_UP_TO_DATE,
	status: 'update-available',
	available_version: '0.2.0',
	detail: 'the channel offers 0.2.0'
};

const REFUSED = {
	...ANSWERED_UP_TO_DATE,
	status: 'endpoint-refused',
	available_version: null,
	detail: 'https://example.invalid/latest.json answered HTTP 404.'
};

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/update-channel.ts');
});

function jsonFetch(status, body) {
	return async () =>
		new Response(JSON.stringify(body), {
			status,
			headers: { 'content-type': 'application/json' }
		});
}

// ----- fetching -----------------------------------------------------------
test('an answered channel yields the verdict', async () => {
	const state = await mod.fetchUpdateCheck(jsonFetch(200, ANSWERED_UP_TO_DATE), '');
	assert.equal(state.kind, 'ok');
	assert.equal(state.value.status, 'up-to-date');
});

test('a 502 is read as a named channel fault, not a transport error', async () => {
	// The engine answers 502 WITH the full check body. Throwing that away
	// would lose the one sentence saying what went wrong.
	const state = await mod.fetchUpdateCheck(jsonFetch(502, REFUSED), '');
	assert.equal(state.kind, 'fault');
	assert.equal(state.value.status, 'endpoint-refused');
	assert.match(state.reason, /404/);
});

test('an unreachable engine is a fault and never up-to-date', async () => {
	const dead = async () => {
		throw new TypeError('Failed to fetch');
	};
	const state = await mod.fetchUpdateCheck(dead, '');
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /Failed to fetch/);
	assert.notEqual(state.kind, 'ok');
});

test('a daemon that predates the route says so rather than looking healthy', async () => {
	const state = await mod.fetchUpdateCheck(jsonFetch(404, { detail: 'Not Found' }), '');
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /predates the update channel/);
});

test('a non-JSON body is a fault carrying the status code', async () => {
	const html = async () => new Response('<html>nope</html>', { status: 200 });
	const state = await mod.fetchUpdateCheck(html, '');
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /not JSON/);
});

test('a 200 without a status field is refused rather than rendered', async () => {
	const state = await mod.fetchUpdateCheck(jsonFetch(200, { endpoint: 'x' }), '');
	assert.equal(state.kind, 'fault');
	assert.match(state.reason, /without a status field/);
});

// ----- summarizing --------------------------------------------------------
test('an available update is prominent and names the version', () => {
	const summary = mod.summarizeUpdate({ kind: 'ok', value: AVAILABLE });
	assert.equal(summary.prominent, true);
	assert.match(summary.label, /0\.2\.0/);
});

test('up to date is not prominent and does not shout', () => {
	const summary = mod.summarizeUpdate({ kind: 'ok', value: ANSWERED_UP_TO_DATE });
	assert.equal(summary.prominent, false);
	assert.equal(summary.label, 'up to date');
});

test('a fault is prominent and explicitly denies being up to date', () => {
	const summary = mod.summarizeUpdate(
		{
			kind: 'fault',
			reason: 'endpoint refused',
			value: REFUSED
		},
		{ updaterExpected: true }
	);
	assert.equal(summary.label, 'UPDATE CHECK FAILED');
	assert.equal(summary.prominent, true);
	assert.match(summary.title, /UNKNOWN/);
	assert.match(summary.title, /not "you are up to date"/);
});

test('a checkout fault is quiet and does not claim up to date', () => {
	const summary = mod.summarizeUpdate(
		{
			kind: 'fault',
			reason: 'https://example.invalid/latest.json answered HTTP 404.',
			value: REFUSED
		},
		{ updaterExpected: false }
	);
	assert.equal(summary.label, 'dev build, no update channel');
	assert.equal(summary.prominent, false);
	assert.match(summary.title, /404/);
	assert.notEqual(summary.label, 'UPDATE CHECK FAILED');
	assert.doesNotMatch(summary.title, /up to date/i);
});

test('an install fault still shouts with the same reason', () => {
	const fault = {
		kind: 'fault',
		reason: 'https://example.invalid/latest.json answered HTTP 404.',
		value: REFUSED
	};
	const summary = mod.summarizeUpdate(fault, { updaterExpected: true });
	assert.equal(summary.label, 'UPDATE CHECK FAILED');
	assert.equal(summary.prominent, true);
	assert.match(summary.title, /UNKNOWN/);
	assert.match(summary.title, /not "you are up to date"/);
	assert.match(summary.title, /404/);
});

test('a network fault on an install survives in the title', () => {
	const summary = mod.summarizeUpdate(
		{
			kind: 'fault',
			reason: 'the engine could not be reached at /api/v1/update/check (Failed to fetch)',
			value: null
		},
		{ updaterExpected: true }
	);
	assert.equal(summary.label, 'UPDATE CHECK FAILED');
	assert.equal(summary.prominent, true);
	assert.match(summary.title, /Failed to fetch/);
});

test('an answered update still shouts on a checkout', () => {
	const summary = mod.summarizeUpdate({ kind: 'ok', value: AVAILABLE }, { updaterExpected: false });
	assert.equal(summary.prominent, true);
	assert.match(summary.label, /0\.2\.0/);
});

test('isUpdaterExpected discriminates dev, repo, payload, and Tauri', () => {
	const cases = [
		{ isDev: true, engineSource: null, inTauri: false, expected: false },
		{ isDev: true, engineSource: 'payload', inTauri: false, expected: false },
		{ isDev: false, engineSource: 'repo', inTauri: false, expected: false },
		{ isDev: false, engineSource: 'payload', inTauri: false, expected: true },
		{ isDev: false, engineSource: null, inTauri: true, expected: true },
		{ isDev: false, engineSource: 'repo', inTauri: true, expected: true },
		{ isDev: false, engineSource: null, inTauri: false, expected: false }
	];
	for (const row of cases) {
		assert.equal(
			mod.isUpdaterExpected(row),
			row.expected,
			`isUpdaterExpected(${JSON.stringify(row)})`
		);
	}
});

test('a same-version local build reads as plain and quiet, never a warning', () => {
	const drifted = { ...ANSWERED_UP_TO_DATE, same_version_different_build: true };
	const summary = mod.summarizeUpdate({ kind: 'ok', value: drifted });
	assert.equal(summary.prominent, false);
	assert.equal(summary.label, 'not the release build');
	assert.doesNotMatch(summary.label, /other build/);
	assert.ok(summary.title.includes(drifted.available_version));
	assert.ok(summary.title.includes(drifted.current_git_sha));
});

test('a build ahead of the channel says so rather than claiming currency', () => {
	const ahead = { ...ANSWERED_UP_TO_DATE, status: 'ahead-of-channel', available_version: '0.0.9' };
	const summary = mod.summarizeUpdate({ kind: 'ok', value: ahead });
	assert.match(summary.label, /ahead of channel/);
	assert.match(summary.title, /channel is behind/);
});

test('no status renders as an empty badge', () => {
	for (const status of [
		'update-available',
		'up-to-date',
		'ahead-of-channel',
		'endpoint-unreachable',
		'endpoint-refused',
		'manifest-malformed',
		'platform-unsupported',
		'identity-unavailable'
	]) {
		const value = { ...ANSWERED_UP_TO_DATE, status };
		const state = mod.isAnswered(status)
			? { kind: 'ok', value }
			: { kind: 'fault', reason: 'x', value };
		const summary = mod.summarizeUpdate(state);
		assert.ok(summary.label.length > 0, `${status} rendered an empty label`);
		assert.ok(summary.title.length > 0, `${status} rendered an empty title`);
	}
	const quiet = mod.summarizeUpdate(
		{ kind: 'fault', reason: 'channel missing', value: REFUSED },
		{ updaterExpected: false }
	);
	assert.ok(quiet.label.length > 0, 'quiet fault rendered an empty label');
	assert.ok(quiet.title.length > 0, 'quiet fault rendered an empty title');
});

test('idle renders nothing at all, which is different from up to date', () => {
	assert.equal(mod.summarizeUpdate({ kind: 'idle' }), null);
});

// ----- applying -----------------------------------------------------------
test('a browser tab reports it cannot install', () => {
	assert.equal(mod.canApplyHere({}), false);
});

test('a Tauri shell reports it can install', () => {
	assert.equal(mod.canApplyHere({ __TAURI_INTERNALS__: {} }), true);
});

test('an Electron shell reports it can install', () => {
	assert.equal(mod.canApplyHere({ opendjShell: { kind: 'electron' } }), true);
});

test('Electron installs through its own bridge, and a bridge rejection is a stated refusal', async () => {
	const phases = [];
	const installed = await mod.applyUpdate((p) => phases.push(p.phase), {
		opendjShell: {
			kind: 'electron',
			applyUpdate: async (onProgress) => {
				onProgress({ phase: 'checking' });
				onProgress({ phase: 'restarting' });
				return { kind: 'installed' };
			}
		}
	});
	assert.deepEqual(installed, { kind: 'installed' });
	assert.deepEqual(phases, ['checking', 'restarting']);

	const refused = await mod.applyUpdate(() => {}, {
		opendjShell: {
			kind: 'electron',
			applyUpdate: async () => {
				throw new Error('this page is not allowed to use the Open DJ shell bridge');
			}
		}
	});
	assert.equal(refused.kind, 'refused');
	assert.match(refused.reason, /not allowed/);
});

test('applying outside a shell is refused with a stated reason', async () => {
	const outcome = await mod.applyUpdate(() => {}, {});
	assert.equal(outcome.kind, 'refused');
	assert.match(outcome.reason, /desktop shell/);
});
