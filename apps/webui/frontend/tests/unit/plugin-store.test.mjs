// requirement: PLUG-6522h, PLUG-6522k
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	badges,
	canSideload,
	installConfirmCopy,
	labsShown,
	listingStatusBadge,
	primaryAction,
	screenshotsFor,
	statusLine
} from '../../src/lib/plugins/plugin-status.ts';
import { memoUntilFailure, slotKey } from '../../src/lib/plugins/slot-keys.ts';

function row(overrides = {}) {
	return {
		plugin_id: 'opendj.b2b-link',
		name: 'B2B Link',
		summary: 's',
		publisher: 'Open DJ',
		trust: 'first-party',
		kind: 'module',
		tier: 0,
		channel: 'stable',
		license: 'Apache-2.0',
		permissions: ['net:lan'],
		notice: null,
		homepage: null,
		available_version: '1.0.0',
		installed_version: null,
		source: null,
		enabled: false,
		load_status: null,
		load_detail: null,
		pending: null,
		update_available: false,
		...overrides
	};
}

test('a store row not installed offers Install, and one with no catalog entry offers nothing', () => {
	assert.equal(primaryAction(row()), 'install');
	assert.equal(primaryAction(row({ available_version: null })), null);
	// An uninstall waiting for a restart offers no Install: that version is still running (Codex on PR #6522).
	assert.equal(primaryAction(row({ pending: 'disable' })), null);
});

test('an installed row offers update before enable or disable', () => {
	const installed = { installed_version: '1.0.0', enabled: true };
	assert.equal(primaryAction(row(installed)), 'disable');
	assert.equal(primaryAction(row({ ...installed, enabled: false })), 'enable');
	assert.equal(primaryAction(row({ ...installed, update_available: true })), 'update');
});

test('pending changes say they apply at the next start, never now', () => {
	assert.match(
		statusLine(row({ pending: 'enable', installed_version: '1.0.0' })),
		/next time Open DJ starts/
	);
	assert.match(
		statusLine(row({ pending: 'disable', installed_version: '1.0.0' })),
		/Turns off next time/
	);
	assert.match(statusLine(row({ pending: 'disable' })), /Removed/);
});

test('a load failure shows its status and the server detail verbatim', () => {
	const line = statusLine(
		row({
			installed_version: '1.0.0',
			enabled: true,
			load_status: 'tampered',
			load_detail: 'x changed after install'
		})
	);
	assert.equal(line, 'Did not load (tampered): x changed after install');
	assert.equal(
		statusLine(row({ installed_version: '1.0.0', enabled: true, load_status: 'loaded' })),
		'Running'
	);
});

test('a data plugin nothing reads yet says so instead of Running', () => {
	const line = statusLine(
		row({ kind: 'data', installed_version: '1.0.0', enabled: true, load_status: 'no-consumer' })
	);
	assert.equal(line, 'Installed - nothing in Open DJ uses this yet');
});

test('a store plugin held off for want of a verified catalog says how to recover', () => {
	const line = statusLine(
		row({ installed_version: '1.0.0', enabled: true, load_status: 'no-catalog', load_detail: 'x' })
	);
	assert.equal(
		line,
		'Off until the store can be checked - refresh the store, then restart Open DJ'
	);
});

test('badges mark labs, third-party terms and unsigned sideloads', () => {
	assert.deepEqual(badges(row()), ['Open DJ']);
	assert.deepEqual(badges(row({ channel: 'labs', tier: 2, source: 'sideload' })), [
		'Open DJ',
		'Labs',
		'Third-party terms',
		'Unsigned'
	]);
	assert.deepEqual(badges(row({ trust: 'third-party', kind: 'data' })), ['Community']);
});

test('screenshots for the current skin come first, then neutral, then the other skin', () => {
	/** if a light-skin user's thumbnail is a dark screenshot while a light one exists then broken */
	const shot = (url, theme) => ({ url, theme, width: 4, height: 3, alt: url });
	const shots = [shot('d1', 'dark'), shot('a1', 'any'), shot('l1', 'light'), shot('d2', 'dark')];
	assert.deepEqual(
		screenshotsFor(shots, 'light').map((s) => s.url),
		['l1', 'a1', 'd1', 'd2']
	);
	assert.deepEqual(
		screenshotsFor(shots, 'dark').map((s) => s.url),
		['d1', 'd2', 'a1', 'l1']
	);
	assert.equal(shots[0].url, 'd1', 'the listing order itself is left alone');
});

test('one image listed for both themes shows once, so gallery keys stay unique (Codex on PR #6522)', () => {
	/** if the same URL comes back twice then the keyed gallery breaks; if distinct images are merged then shots go missing */
	const shot = (url, theme) => ({ url, theme, width: 4, height: 3, alt: `${url} ${theme}` });
	const shots = [shot('same', 'dark'), shot('same', 'light'), shot('other', 'any')];
	const light = screenshotsFor(shots, 'light');
	assert.deepEqual(
		light.map((s) => s.url),
		['same', 'other']
	);
	assert.equal(light[0].theme, 'light', 'the copy tagged for this theme is the one kept');
	assert.equal(screenshotsFor(shots, 'dark')[0].theme, 'dark');
});

test('only a non-stable listing status gets a badge', () => {
	/** if a stable module carries a "Stable" badge or an experiment carries none then broken */
	assert.equal(listingStatusBadge('stable'), null);
	assert.equal(listingStatusBadge('experiment'), 'Experiment');
	assert.equal(listingStatusBadge('beta'), 'Beta');
	assert.equal(listingStatusBadge('deprecated'), 'Deprecated');
});

test('sideload is offered only for a previewed build, and with terms only once accepted (Codex on PR #6522)', () => {
	const preview = {
		plugin_id: 'opendj.x',
		name: 'X',
		version: '1.0.0',
		channel: 'labs',
		permissions: [],
		notice: null,
		manifest_sha256: 'a'.repeat(64)
	};
	assert.equal(canSideload(null, true), false, 'nothing previewed: nothing to consent to');
	assert.equal(canSideload(preview, false), true, 'no terms to accept');
	const withTerms = { ...preview, notice: 'Third-party terms.' };
	assert.equal(canSideload(withTerms, false), false);
	assert.equal(canSideload(withTerms, true), true);
});

test('a failed UI-slot load is retried on the next call, a good one is shared (Codex on PR #6522)', async () => {
	let calls = 0;
	let fail = true;
	const load = memoUntilFailure(async () => {
		calls += 1;
		if (fail) throw new Error('engine restarting');
	});
	await assert.rejects(load(), /engine restarting/);
	fail = false;
	await load();
	assert.equal(calls, 2, 'the failure was cached instead of retried');
	await load();
	await Promise.all([load(), load()]);
	assert.equal(calls, 2, 'a successful load must be shared, not repeated'); // control
});

test('slot keys never collide across plugin id and version (Codex on PR #6522)', () => {
	const a = slotKey({ plugin_id: 'opendj.a1', version: '2.3.0' });
	const b = slotKey({ plugin_id: 'opendj.a', version: '12.3.0' });
	assert.notEqual(a, b);
	assert.equal(slotKey({ plugin_id: 'opendj.a', version: '1.0.0' }), 'opendj.a@1.0.0');
});

test('hiding labs keeps installed labs modules and their Uninstall on the page', () => {
	const rows = [
		{ plugin_id: 'opendj.on', installed_version: '1.0.0' },
		{ plugin_id: 'opendj.off', installed_version: null }
	];
	assert.deepEqual(
		labsShown(rows, false).map((r) => r.plugin_id),
		['opendj.on']
	);
	assert.deepEqual(
		labsShown(rows, true).map((r) => r.plugin_id),
		['opendj.on', 'opendj.off']
	); // control
});

test('the update confirmation says update and never promises a disabled module turns on', () => {
	const off = { name: 'Mod', enabled: false, available_version: '1.1.0' };
	const copy = installConfirmCopy(off, 'update');
	assert.equal(copy.title, 'Update Mod to 1.1.0?');
	assert.equal(copy.button, 'Update');
	assert.doesNotMatch(copy.line, /turns on/);
	assert.match(installConfirmCopy({ ...off, enabled: true }, 'update').line, /takes over/);
	assert.match(installConfirmCopy(off, 'install').line, /turns on/); // control: a fresh install does
});
