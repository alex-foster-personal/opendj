// requirement: CMDK-01
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/command-bar.ts');
});

function row(stable_id, title, artist, extra = {}) {
	return {
		stable_id,
		title,
		artist,
		genre: null,
		key: null,
		bpm: null,
		file_exists: true,
		is_streaming: false,
		...extra
	};
}

const ROWS = [
	row('a', 'One More Time', 'Daft Punk', { genre: 'House', key: '8A' }),
	row('b', 'Around the World', 'Daft Punk'),
	row('c', 'Strings of Life', 'Rhythim Is Rhythim', { genre: 'Techno' })
];

test('every token must match, in any order and any field', () => {
	const hay = ROWS.map(mod.rowHaystack);
	assert.deepEqual(
		mod.matchRows(ROWS, hay, 'punk one').rows.map((r) => r.stable_id),
		['a']
	);
	assert.deepEqual(
		mod.matchRows(ROWS, hay, 'TECHNO').rows.map((r) => r.stable_id),
		['c']
	);
	// The overshoot to guard: matching ANY token would return both Daft Punk rows.
	assert.equal(mod.matchRows(ROWS, hay, 'daft strings').total, 0);
});

test('an empty query lists the playlist as it stands', () => {
	const hay = ROWS.map(mod.rowHaystack);
	const result = mod.matchRows(ROWS, hay, '   ');
	assert.deepEqual(result.rows.map((r) => r.stable_id), ['a', 'b', 'c']);
	assert.equal(result.total, 3);
});

test('the list is cut at the limit but the total counts every match', () => {
	const many = Array.from({ length: 120 }, (_, i) => row(`r${i}`, `Track ${i}`, 'Same'));
	const result = mod.matchRows(many, many.map(mod.rowHaystack), 'same', 50);
	assert.equal(result.rows.length, 50);
	assert.equal(result.total, 120);
});

test('haystacks out of step with rows fail loudly', () => {
	assert.throws(() => mod.matchRows(ROWS, [], 'x'), /0 haystacks for 3 rows/);
});

test('default deck: lowest empty, else lowest non-master', () => {
	const d = (id, loaded, is_master = false) => ({ id, loaded, is_master });
	assert.equal(mod.defaultTargetDeck([d(1, true), d(2, false), d(3, false), d(4, false)]), 2);
	assert.equal(mod.defaultTargetDeck([d(1, true, true), d(2, true), d(3, true), d(4, true)]), 2);
	assert.equal(mod.defaultTargetDeck([d(1, true), d(2, true), d(3, true), d(4, true)]), 1);
});

test('deck and row stepping stop at the ends', () => {
	assert.equal(mod.stepDeck([1, 2, 3, 4], 4, 1), 4);
	assert.equal(mod.stepDeck([1, 2, 3, 4], 1, -1), 1);
	assert.equal(mod.stepDeck([1, 2, 3, 4], 2, 1), 3);
	assert.equal(mod.stepSelection(0, 5, -1), 0);
	assert.equal(mod.stepSelection(4, 5, 1), 4);
	assert.equal(mod.stepSelection(0, 0, 1), 0);
});

test('the bar loads through the browser deck-load path, not its own', () => {
	const page = readFileSync(`${SRC}/routes/performance/+page.svelte`, 'utf8');
	assert.match(page, /<CommandBar[\s\S]{0,200}load=\{browserPanel\.commandBarLoad\}/);
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	assert.match(panel, /export async function commandBarLoad[\s\S]{0,120}await _loadOntoDeck\(row, deck\)/);
	const bar = readFileSync(`${SRC}/lib/components/rb/CommandBar.svelte`, 'utf8');
	assert.doesNotMatch(bar, /dispatchPerformanceCommand|runPerformanceCommandFromUi/);
});

// requirement: CMDK-03
function fakeClock() {
	let t = 0;
	return { now: () => t, sleep: async (ms) => { t += ms; } };
}

test('vocals-only waits while stems are loading, then reports ready', async () => {
	const clock = fakeClock();
	const states = ['loading', 'loading', 'ready'];
	let i = 0;
	const read = () => ({ stable_id: 'a', status: states[Math.min(i++, states.length - 1)] });
	assert.equal(await mod.waitForStemsSettled(read, 'a', clock), 'ready');
	assert.equal(i, 3);
});

test('vocals-only stops at once when the deck moves to another track', async () => {
	// The overshoot to guard: soloing whatever is on the deck after a long wait.
	const clock = fakeClock();
	let i = 0;
	const read = () => ({ stable_id: i++ < 2 ? 'a' : 'b', status: 'loading' });
	assert.equal(await mod.waitForStemsSettled(read, 'a', clock), 'stale');
});

test('vocals-only reports a settled no-stems or error state without waiting', async () => {
	const clock = fakeClock();
	assert.equal(await mod.waitForStemsSettled(() => ({ stable_id: 'a', status: 'unavailable' }), 'a', clock), 'unavailable');
	assert.equal(await mod.waitForStemsSettled(() => ({ stable_id: 'a', status: 'error' }), 'a', clock), 'error');
	assert.equal(clock.now(), 0);
});

test('vocals-only gives up after its ceiling', async () => {
	const clock = fakeClock();
	const read = () => ({ stable_id: 'a', status: 'loading' });
	assert.equal(await mod.waitForStemsSettled(read, 'a', { ...clock, maxWaitMs: 1000 }), 'timeout');
	assert.ok(clock.now() >= 1000);
});

test('the bar hands the stems result on only after they settle', () => {
	const bar = readFileSync(`${SRC}/lib/components/rb/CommandBar.svelte`, 'utf8');
	const choose = bar.slice(bar.indexOf('async function choose'));
	const wait = choose.indexOf('await waitForStemsSettled(');
	const solo = choose.indexOf('await soloVocals(');
	assert.ok(wait > 0 && solo > wait, 'soloVocals must follow the stems wait');
	assert.match(choose, /if \(settled !== 'stale'\) await soloVocals/);
});

test('the browser solos the vocal stem only when the stems are ready', () => {
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	const hook = panel.slice(panel.indexOf('export async function commandBarSoloVocals'));
	const notReady = hook.indexOf("if (settled !== 'ready')");
	const solo = hook.indexOf("type: 'stem_solo'");
	assert.ok(notReady > 0 && solo > notReady, 'stem_solo must sit behind the ready check');
});
