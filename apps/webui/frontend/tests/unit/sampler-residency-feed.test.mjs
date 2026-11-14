// requirement: EDGESAMP-14631
/**
 * Issue #10782 (S5): what feeds the residency plan. The store's edge state read as a residency
 * view (awake edges' tabs and neighbour pages, intent edges' tabs), the throttled latest-wins
 * runner that promotes it, and the gate tab's press (fire on down, release once on let-go).
 *
 * Regression lines:
 * - if an awake edge's view misses its tabs or the sets either side then a flip plays cold clips
 * - if both edges sleep with no intent and the view is not closed then a closed panel does work
 * - if an asleep edge with intent contributes neighbours then the closed panel fetches two extra pages
 * - if result pages are not read for an edge showing search results then the view names the set instead
 * - if a closed view arms a timer or runs then the closed panel is not idle
 * - if views inside the throttle each run then a wake promotes the pool many times per frame
 * - if a gate press releases twice or releases the flipped-in slot then the wrong voice stops
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let F;
let G;
before(async () => {
	F = await loadTypeScriptModule('src/lib/player/sampler/sampler-residency-feed.ts');
	G = await loadTypeScriptModule('src/lib/player/sampler/sampler-gate-press.ts');
});

const s = (id) => ({ slot_id: id });
const ids = (slots) => slots.map((x) => x.slot_id);
const edge = (o = {}) => ({ set: 0, results: null, rpage: 0, awake: false, intent: false, ...o });
/** Four sets of two tabs: set i holds `i-a` and `i-b` (set 2 has an empty second slot). */
const store = (L = {}, R = {}) => ({
	perSet: 2,
	sets: [0, 1, 2, 3].map((i) => ({ items: [s(`${i}-a`), i === 2 ? null : s(`${i}-b`)] })),
	edges: { L: edge(L), R: edge(R) }
});

test('awake edge view', () => {
	/** [if] the L edge is awake on set 1 [then] set 1 is visible in tab order and sets 0 and 2 are neighbours, [else stop]. */
	const v = F.residencyInputs(store({ set: 1, awake: true }));
	assert.equal(v.open, true);
	assert.equal(v.intent, false);
	assert.deepEqual(ids(v.visible), ['1-a', '1-b']);
	assert.deepEqual(ids(v.neighbours), ['0-a', '0-b', '2-a']);
});

test('pages wrap', () => {
	/** [if] the R edge is awake on the first set [then] its neighbours are the last set and the second, [else stop]. */
	const v = F.residencyInputs(store({}, { set: 0, awake: true }));
	assert.deepEqual(ids(v.visible), ['0-a', '0-b']);
	assert.deepEqual(ids(v.neighbours), ['3-a', '3-b', '1-a', '1-b']);
});

test('closed view', () => {
	/** [if] both edges sleep with no intent [then] the view is closed with nothing visible or neighbouring, [else stop]. */
	const v = F.residencyInputs(store({ set: 1 }, { set: 3 }));
	assert.deepEqual(v, { open: false, intent: false, visible: [], neighbours: [] });
});

test('intent edge view', () => {
	/** [if] only the R edge has intent [then] its visible tabs are named, with no neighbours and the panel closed, [else stop]. */
	const v = F.residencyInputs(store({ set: 1 }, { set: 3, intent: true }));
	assert.equal(v.open, false);
	assert.equal(v.intent, true);
	assert.deepEqual(ids(v.visible), ['3-a', '3-b']);
	assert.deepEqual(v.neighbours, []);
});

test('search results pages', () => {
	/** [if] an awake edge shows page 1 of five results [then] results 3-4 are visible, 1-2 and 5 neighbours, [else stop]. */
	const results = ['r1', 'r2', 'r3', 'r4', 'r5'].map(s);
	const v = F.residencyInputs(store({ awake: true, results, rpage: 1 }));
	assert.deepEqual(ids(v.visible), ['r3', 'r4']);
	assert.deepEqual(ids(v.neighbours), ['r1', 'r2', 'r5']);
});

/** A fake clock: timers fire only when the test calls `advance`. */
function fakeClock() {
	let now = 0;
	const timers = new Map();
	let next = 1;
	return {
		timers,
		now: () => now,
		setTimeout: (fn, ms) => {
			timers.set(next, { fn, at: now + ms });
			return next++;
		},
		clearTimeout: (h) => void timers.delete(h),
		async advance(ms) {
			now += ms;
			for (const [h, t] of [...timers]) {
				if (t.at > now) continue;
				timers.delete(h);
				t.fn();
			}
			await new Promise((r) => setImmediate(r));
		}
	};
}

const view = (id, open = true) => ({ open, intent: false, visible: [s(id)], neighbours: [] });

test('closed view is idle', async () => {
	/** [if] only closed views are fed [then] no timer is armed and the run never happens, [else stop]. */
	const clock = fakeClock();
	const runs = [];
	const feed = F.createResidencyFeed(async (v) => void runs.push(v), clock, 50);
	feed(view('a', false));
	feed({ open: false, intent: false, visible: [], neighbours: [] });
	await clock.advance(1000);
	assert.equal(clock.timers.size, 0);
	assert.deepEqual(runs, []);
});

test('throttled latest wins', async () => {
	/** [if] three views arrive inside the throttle [then] the first runs at once, only the last runs after it, [else stop]. */
	const clock = fakeClock();
	const runs = [];
	const feed = F.createResidencyFeed(async (v) => void runs.push(ids(v.visible)[0]), clock, 50);
	feed(view('a'));
	await clock.advance(0);
	feed(view('b'));
	feed(view('c'));
	assert.deepEqual(runs, ['a']);
	await clock.advance(50);
	assert.deepEqual(runs, ['a', 'c']);
	assert.equal(clock.timers.size, 0);
});

test('closing cancels a pending run', async () => {
	/** [if] the panel closes while a throttled run waits [then] the timer is cleared and that run never happens, [else stop]. */
	const clock = fakeClock();
	const runs = [];
	const feed = F.createResidencyFeed(async (v) => void runs.push(ids(v.visible)[0]), clock, 50);
	feed(view('a'));
	await clock.advance(0);
	feed(view('b'));
	feed(view('b', false));
	await clock.advance(100);
	assert.deepEqual(runs, ['a']);
	assert.equal(clock.timers.size, 0);
});

test('one run at a time', async () => {
	/** [if] a view arrives while a run is still going [then] it runs once that run ends, never alongside it, [else stop]. */
	const clock = fakeClock();
	let release;
	let live = 0;
	let maxLive = 0;
	const runs = [];
	const feed = F.createResidencyFeed(
		async (v) => {
			live++;
			maxLive = Math.max(maxLive, live);
			runs.push(ids(v.visible)[0]);
			if (runs.length === 1) await new Promise((r) => (release = r));
			live--;
		},
		clock,
		50
	);
	feed(view('a'));
	await clock.advance(100);
	feed(view('b'));
	await clock.advance(100);
	assert.deepEqual(runs, ['a']);
	release();
	await clock.advance(0);
	await clock.advance(100);
	assert.deepEqual(runs, ['a', 'b']);
	assert.equal(maxLive, 1);
});

test('gate press releases once, the pressed slot', () => {
	/** [if] up, cancel and lost capture follow one down after the tab flipped [then] the pressed slot releases once, [else stop]. */
	const calls = [];
	const gate = G.createGatePress(
		(slot) => calls.push(['fire', slot.slot_id]),
		(id) => calls.push(['release', id])
	);
	gate.up();
	gate.down(s('g1'));
	gate.down(s('g2'));
	gate.up();
	gate.up();
	gate.up();
	assert.deepEqual(calls, [
		['fire', 'g1'],
		['release', 'g1']
	]);
});
