/**
 * REFRESH-01 (issue #891): a full page reload while the maintainer is looking at the tab
 * gets a 3 s (3-2-1) on-top countdown first, so he can screenshot or finish what he
 * is doing. A reload while the tab is hidden happens immediately - a countdown
 * nobody can see is just latency.
 *
 * The scheduler is pure and takes its effects, so this drives it with a fake
 * clock and a fake reload rather than waiting real seconds.
 *
 * Regression lines:
 * - if a visible reload stops counting down then the page vanishes mid-thought
 *   again, which is the whole complaint
 * - if a hidden tab starts counting then a background reload is held up for
 *   seconds for nobody
 * - if the default length drifts off 3 then the 3-2-1 the maintainer asked for
 *   (Thu 1 Oct 2026) is gone
 * - if a second trigger restarts or stacks the countdown then two announcements
 *   race and the deadline moves while it is being read
 * - if the last tick does not reload then the overlay is a dead end
 * - if the tab is backgrounded mid-count then the countdown keeps ticking
 *   instead of completing at once, holding up a reload nobody can see
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/reload-countdown.ts');
});

/** Fake clock: `tick()` advances one whole second. */
function harness({ visible = true } = {}) {
	const rendered = [];
	let cleared = 0;
	let reloads = 0;
	let cb = null;
	let hiddenCb = null;
	const scheduler = mod.createReloadScheduler({
		isVisible: () => visible,
		reload: () => {
			reloads += 1;
		},
		everySecond: (fn) => {
			cb = fn;
			return () => {
				cb = null;
			};
		},
		onHidden: (fn) => {
			hiddenCb = fn;
			return () => {
				hiddenCb = null;
			};
		},
		render: (secondsLeft, reason) => rendered.push(`${secondsLeft}:${reason}`),
		clear: () => {
			cleared += 1;
		}
	});
	return {
		scheduler,
		rendered,
		tick: () => {
			assert.ok(cb !== null, 'no countdown is running');
			cb();
		},
		hide: () => {
			assert.ok(hiddenCb !== null, 'no hidden-listener is registered');
			hiddenCb();
		},
		get running() {
			return cb !== null;
		},
		get hiddenListening() {
			return hiddenCb !== null;
		},
		get reloads() {
			return reloads;
		},
		get cleared() {
			return cleared;
		}
	};
}

describe('reload countdown', () => {
	it('counts 3-2-1 down to 0 while the tab is visible, then reloads', () => {
		const h = harness();
		h.scheduler.schedule('vite full reload');
		assert.equal(h.reloads, 0, 'reloaded before the countdown even started');
		assert.deepEqual(h.rendered, ['3:vite full reload']);
		h.tick();
		h.tick();
		assert.equal(h.reloads, 0, 'reloaded before the last tick');
		h.tick();
		assert.deepEqual(
			h.rendered.map((r) => Number(r.split(':')[0])),
			[3, 2, 1, 0]
		);
		assert.equal(h.reloads, 1);
		assert.equal(h.cleared, 1, 'the overlay was left on screen');
		assert.equal(h.running, false, 'the timer was left running');
	});

	it('reloads immediately with no overlay when the tab is hidden', () => {
		const h = harness({ visible: false });
		h.scheduler.schedule('engine restarted');
		assert.equal(h.reloads, 1);
		assert.deepEqual(h.rendered, [], 'rendered a countdown nobody can see');
		assert.equal(h.running, false);
	});

	it('a second trigger joins the countdown rather than starting another', () => {
		const h = harness();
		h.scheduler.schedule('first');
		h.tick();
		h.scheduler.schedule('second');
		// Still counting down from the FIRST deadline: the number a reader is
		// looking at must not jump back up.
		assert.deepEqual(h.rendered, ['3:first', '2:first']);
		for (let i = 0; i < 2; i += 1) h.tick();
		assert.equal(h.reloads, 1, 'two countdowns reloaded twice');
	});

	it('takes a custom length, and treats a non-positive one as immediate', () => {
		const h = harness();
		h.scheduler.schedule('quick', 2);
		assert.deepEqual(h.rendered, ['2:quick']);
		h.tick();
		h.tick();
		assert.equal(h.reloads, 1);

		const now = harness();
		now.scheduler.schedule('no wait', 0);
		assert.equal(now.reloads, 1);
		assert.deepEqual(now.rendered, []);
	});

	it('publishes the default length rather than burying it', () => {
		assert.equal(mod.RELOAD_COUNTDOWN_S, 3);
	});

	it('completes at once, not on a delay, when the tab is backgrounded mid-count', () => {
		const h = harness();
		h.scheduler.schedule('vite full reload');
		h.tick();
		h.tick();
		assert.equal(h.hiddenListening, true, 'no hidden-listener registered while counting');
		h.hide();
		assert.equal(h.reloads, 1, 'a hidden tab should not wait out the remaining ticks');
		assert.equal(h.cleared, 1);
		assert.equal(h.running, false, 'the timer was left running after the hidden reload');
		assert.equal(h.hiddenListening, false, 'the hidden-listener was left registered after firing');
	});

	it('unregisters the hidden-listener once a countdown finishes normally', () => {
		const h = harness();
		h.scheduler.schedule('quick', 1);
		h.tick();
		assert.equal(h.hiddenListening, false, 'the hidden-listener outlived its own countdown');
	});

	it('unregisters the hidden-listener when a countdown is cancelled', () => {
		const h = harness();
		h.scheduler.schedule('first');
		h.scheduler.cancel();
		assert.equal(h.hiddenListening, false, 'the hidden-listener outlived cancel()');
	});
});
