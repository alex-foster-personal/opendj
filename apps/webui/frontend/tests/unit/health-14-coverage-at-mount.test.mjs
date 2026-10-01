/**
 * HEALTH-14: the coverage lights ask at mount, and the Library light names
 * what it is waiting on.
 *
 * Measured before (Thu 1 Oct 2026, live preview): the cached coverage read
 * answers in 9 ms, but `_init()` only sent it after the whole paged listing
 * finished, about 17.6 s after load in a visible tab and 33 to 41 s in a
 * hidden one. The request now leaves from onMount, beside `_init()`.
 *
 * Regression lines:
 *   - if the coverage request is sent from the tail of _init() then broken
 *   - if onMount does not send the coverage request before _init() then broken
 *   - if the reconcile summary moves out of _init()'s finally then broken (#3750)
 *   - if the Library light shows no row counts while the listing loads then broken
 *   - if a load-progress figure overrides a settled or failed verdict then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

function stripComments(src) {
	return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
}

let dots;

before(async () => {
	dots = await loadTypeScriptModule('src/lib/rb/library-health-dots.ts');
});

const AVAILABILITY = {
	total: 10,
	present: 8,
	broken_here: 0,
	off_machine: 2,
	awaiting_volume: 0,
	streaming: 0,
	pathless: 0
};

test('onMount sends the coverage request before _init(), and _init() no longer sends it', () => {
	const src = stripComments(readFileSync(PANEL, 'utf8'));
	const onMountBlock = src.match(/onMount\(\(\) => \{[\s\S]*?\n\t\}\);/)?.[0] ?? '';
	assert.match(
		onMountBlock,
		/void _loadIngestCoverage\(\);\s*void _init\(\);/,
		'the cached coverage read must leave at mount, ahead of the listing'
	);
	const initBody = src.match(/async function _init\(\): Promise<void> \{[\s\S]*?\n\t\}\n/)?.[0] ?? '';
	assert.ok(initBody.length > 200, 'control: the _init() body was found');
	assert.match(initBody, /void _loadReconcileSummary\(\);/, 'control: the probe sees calls in _init()');
	assert.doesNotMatch(initBody, /_loadIngestCoverage/);
});

test('the Library light names loaded and total rows while the listing is in flight', () => {
	const dot = dots.libraryHealthDot(null, 8355, 2, null, null, { loaded: 3000, total: 8355 });
	assert.equal(dot.state, 'loading');
	assert.equal(dot.detail, 'loading 3000 of 8355 tracks; which are on this machine is checked next');
});

test('before the total is known the Library light names the rows received', () => {
	const dot = dots.libraryHealthDot(null, null, 0, null, null, { loaded: 500, total: null });
	assert.equal(dot.state, 'loading');
	assert.match(dot.detail, /^loading 500 tracks, total not yet known/);
});

test('with no listing in flight the wording is the earlier one', () => {
	assert.equal(
		dots.libraryHealthDot(null, 8355, 2, null, null, null).detail,
		'checking which tracks are on this machine'
	);
	assert.equal(dots.libraryHealthDot(null, 8355, 2, null, null).detail, 'checking which tracks are on this machine');
});

test('overshoot control: load progress never replaces a verdict, an unknown or an error', () => {
	const progress = { loaded: 3000, total: 8355 };
	assert.equal(dots.libraryHealthDot(null, 10, 2, AVAILABILITY, null, progress).state, 'complete');
	assert.equal(dots.libraryHealthDot('engine down', 10, 2, null, null, progress).state, 'error');
	assert.equal(dots.libraryHealthDot(null, 10, 2, null, 'summary timed out', progress).state, 'unavailable');
});
