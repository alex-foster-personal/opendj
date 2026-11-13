// requirement: EDGESAMP-14346
/**
 * Issue #10785 (S8): an edge tab's triggers (play, preview wave, repeat, pencil, pin) count a
 * click only on a readable, settled tab, and drop a bounce on the same tab (spec 7.2).
 *
 * Regression lines:
 * - if a click on a tab under 150 px wide fires then a sweep across a waking edge plays samples
 * - if a click within 150 ms of the edge waking fires then the opening wave triggers tabs
 * - if the same tab fires twice within 120 ms then a bouncing mouse double-fires a sample
 * - if one tab's click debounces another tab then fast two-tab play is lost
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let G;
let wave;
before(async () => {
	G = await loadTypeScriptModule('src/lib/player/sampler/sampler-trigger-guards.ts');
	wave = await loadTypeScriptModule('src/lib/player/sampler/edge-wave.ts');
});

const click = (over) => ({ key: 's1', widthPx: 476, msSinceWake: 400, now: 1000, ...over });

test('a narrow or just-woken tab ignores the click', () => {
	/** [if] a tab is 149 px wide or the edge woke 149 ms ago [then] the verdict refuses it, 150/150 passes, [else stop]. */
	assert.equal(wave.CLICK_MIN_W, 150);
	assert.equal(wave.CLICK_SETTLE_MS, 150);
	const v = (over) => G.triggerVerdict(click(over), undefined);
	assert.equal(v({ widthPx: 149 }), 'too-narrow-or-settling');
	assert.equal(v({ msSinceWake: 149 }), 'too-narrow-or-settling');
	assert.equal(v({ widthPx: 150, msSinceWake: 150 }), 'ok');
	const guard = G.createTriggerGuard();
	assert.equal(guard.accept(click({ widthPx: 32 })), false);
	assert.equal(guard.accept(click({ now: 1001 })), true, 'a refused click starts no debounce');
});

test('the same tab twice within 120 ms is debounced', () => {
	/** [if] tab s1 is clicked at 1000, 1119 and 1240 ms [then] 1000 and 1240 count and 1119 is dropped, [else stop]. */
	assert.equal(G.TAB_DEBOUNCE_MS, 120);
	const guard = G.createTriggerGuard();
	const got = [1000, 1119, 1240].map((now) => guard.accept(click({ now })));
	assert.deepEqual(got, [true, false, true]);
	assert.equal(G.triggerVerdict(click({ now: 1119 }), 1000), 'debounced');
	assert.equal(G.triggerVerdict(click({ now: 1120 }), 1000), 'ok');
});

test('a click on one tab never debounces another', () => {
	/** [if] tab s1 then tab s2 are clicked 10 ms apart [then] both count, [else stop]. */
	const guard = G.createTriggerGuard();
	assert.equal(guard.accept(click({ key: 's1', now: 1000 })), true);
	assert.equal(guard.accept(click({ key: 's2', now: 1010 })), true);
	assert.equal(guard.accept(click({ key: 's1', now: 1020 })), false);
});
