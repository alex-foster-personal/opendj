// requirement: LIBUX-8679, LIBUX-8679b, LIBUX-8679c
// LIBUX-8679: the frontend mirror runs the same parity cases as
// tests/shared/test_set_target_core.py, and the header readout renders.
// [if] played length, Camelot clash or summary math drifts from the backend [then] fail.
// [if] the readout does not show "~Xm of Ym · Zm left/over" [then] fail.
// [if] the skip shortcut is not Cmd/Ctrl+Shift+X [then] fail.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const CASES = JSON.parse(readFileSync(join(here, '../../../../shared/set_target/parity_cases.json'), 'utf8'));
const ENTRY = [
	"export { default as Readout } from '$lib/components/rb/browser/SetTargetReadout.svelte';",
	"export { playedLengthS, keyClash, summarizeSet, formatReadout } from '$lib/rb/set-target';",
	"export { isSetSkipShortcut } from '$lib/rb/set-target-state.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

const close = (a, b) => Math.abs(a - b) < 1e-6;

test('played length matches the backend parity cases', () => {
	for (const c of CASES.played) {
		const got = mod.playedLengthS(c.duration_s, c.bpm, c.mix_in_s ?? null, c.mix_out_s ?? null);
		assert.ok(close(got, c.expect), `${JSON.stringify(c)} -> ${got}`);
	}
});

test('key clash matches the backend parity cases', () => {
	for (const c of CASES.clash) assert.deepEqual(mod.keyClash(c.from, c.to), c.expect, JSON.stringify(c));
});

test('summary and readout match the backend parity cases', () => {
	for (const c of CASES.summary) {
		const { readout, ...expect } = c.expect;
		const got = mod.summarizeSet(c.entries, c.target_s);
		assert.deepEqual(got, expect);
		assert.equal(mod.formatReadout(got.played_s, c.target_s), readout);
	}
	assert.equal(mod.formatReadout(67 * 60, 3600), '~67m of 60m · 7m over');
	assert.equal(mod.formatReadout(150, null), '~2m');
});

test('readout renders the server summary and marks over-target', () => {
	const summary = { playlist_id: 'p', played_s: 4020, target_s: 3600, over_s: 420, skipped: 2, skipped_ids: [],
		clashes: [], readout: '~67m of 60m · 7m over' };
	const body = mod.render(mod.Readout, { props: { summary, onsettarget: () => {} } }).body;
	assert.match(body, /data-testid="set-target-readout"/);
	assert.match(body, /~67m of 60m · 7m over/);
	assert.match(body, /class="set-target-readout[^"]*over/);
	assert.match(body, /2 skipped/);
	const none = mod.render(mod.Readout, { props: { summary: null, onsettarget: () => {} } }).body;
	assert.doesNotMatch(none, /set-target-readout/);
});

test('skip shortcut is Cmd/Ctrl+Shift+X only', () => {
	const base = { key: 'X', shiftKey: true, metaKey: true, ctrlKey: false, altKey: false };
	assert.equal(mod.isSetSkipShortcut(base), true);
	assert.equal(mod.isSetSkipShortcut({ ...base, metaKey: false, ctrlKey: true }), true);
	assert.equal(mod.isSetSkipShortcut({ ...base, shiftKey: false }), false);
	assert.equal(mod.isSetSkipShortcut({ ...base, altKey: true }), false);
});

test('TrackTable dims skipped rows by opacity only', () => {
	const table = readFileSync(join(here, '../../src/lib/components/rb/browser/TrackTable.svelte'), 'utf8');
	assert.match(table, /class:rb-row-set-skip=\{setTargetState\.skipIds\.has\(row\.stable_id\)\}/);
	const rule = /\.rb-row-set-skip\s*\{([^}]*)\}/.exec(table);
	assert.ok(rule, 'rb-row-set-skip style rule');
	assert.match(rule[1], /opacity:/);
	assert.doesNotMatch(rule[1], /line-through|display|order/);
});
