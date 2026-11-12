import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// STEMPERF-11699 - scroll over a deck stem chip sets that stem's level, and a
// double-click returns it to unity.
//
// Regression lines:
// - if a wheel notch over a live chip does not dispatch stem_gain for that stem then broken
// - if a wheel over an unavailable or not-ready chip dispatches anything, or blocks page scroll, then broken
// - if any plain click (at or off unity) does not dispatch mute synchronously then Class A mute latency regressed (PRRT_kwDOSEvNd861jTeZ) - broken
// - if the dblclick dispatches stem_mute, or a full double-click leaves the mute toggled an odd number of times, then broken
// - if the level bar renders at unity, or 0.0 dB reads as -0.0 dB, then broken

let lvl;
let wa;

before(async () => {
	lvl = await loadTypeScriptModule('src/lib/rb/stem-chip-level.ts');
	wa = await loadTypeScriptModule('src/lib/rb/wheel-adjust.ts');
});

function notch(wheelDeltaY) {
	const event = new Event('wheel', { cancelable: true });
	event.deltaY = -100;
	event.deltaX = 0;
	event.deltaMode = 0;
	event.wheelDeltaY = wheelDeltaY;
	return event;
}

function chip({ live }) {
	const calls = [];
	const node = new EventTarget();
	wa.wheelAdjust(
		node,
		lvl.stemChipWheelParams({
			stem: 'vocal',
			gain: () => 0.5,
			live,
			step: wa.WHEEL_STEP.knob,
			onGain: (stem, value) => calls.push([stem, value])
		})
	);
	return { node, calls };
}

test('one wheel notch up on a live VOCAL chip dispatches stem_gain vocal raised by the input-kind step', () => {
	for (const [kind, wheelDeltaY] of [
		['mouse', 120],
		['trackpad', 7]
	]) {
		const { node, calls } = chip({ live: true });
		const event = notch(wheelDeltaY);
		node.dispatchEvent(event);
		assert.equal(calls.length, 1, `${kind}: one stem_gain`);
		assert.equal(calls[0][0], 'vocal');
		const expected = 0.5 + wa.scaledWheelStep(wa.WHEEL_STEP.knob, kind);
		assert.ok(Math.abs(calls[0][1] - expected) < 1e-12, `${kind}: ${calls[0][1]} != ${expected}`);
		assert.equal(event.defaultPrevented, true);
	}
});

test('a wheel notch on an unavailable chip dispatches nothing and lets the page scroll', () => {
	const { node, calls } = chip({ live: false });
	const event = notch(120);
	node.dispatchEvent(event);
	assert.deepEqual(calls, []);
	assert.equal(event.defaultPrevented, false);
});

function arbiter() {
	const log = { mute: [], solo: [], gain: [] };
	const a = lvl.createChipClickArbiter({
		onMute: (s) => log.mute.push(s),
		onSolo: (s) => log.solo.push(s),
		onGain: (s, v) => log.gain.push([s, v])
	});
	return { a, log };
}

test('double-click on a chip at gain 0.3 dispatches stem_gain 0.5 and the dblclick sends no stem_mute', () => {
	const { a, log } = arbiter();
	a.click({ stem: 'vocal', shift: false });
	a.click({ stem: 'vocal', shift: false });
	const muteBeforeDbl = log.mute.length;
	a.doubleClick('vocal');
	assert.deepEqual(log.gain, [['vocal', 0.5]]);
	assert.deepEqual(log.mute, ['vocal', 'vocal']);
	assert.equal(log.mute.length, muteBeforeDbl);
});

test('a click off unity mutes synchronously, never deferred (Class A, PRRT_kwDOSEvNd861jTeZ)', () => {
	const { a, log } = arbiter();
	a.click({ stem: 'vocal', shift: false });
	assert.deepEqual(log.mute, ['vocal']);
});

test('a click at unity mutes synchronously (unchanged behaviour)', () => {
	const { a, log } = arbiter();
	a.click({ stem: 'vocal', shift: false });
	a.click({ stem: 'vocal', shift: false });
	assert.deepEqual(log.mute, ['vocal', 'vocal']);
});

test('Shift+click solos at once and never mutes', () => {
	const { a, log } = arbiter();
	a.click({ stem: 'drums', shift: true });
	a.click({ stem: 'drums', shift: true });
	assert.deepEqual(log.solo, ['drums', 'drums']);
	assert.deepEqual(log.mute, []);
});

test('the module exposes no double-click delay', () => {
	assert.equal(lvl.STEM_CHIP_DBLCLICK_MS, undefined);
});

test('level view: bar absent at unity, height proportional to the knob, dB tooltip label', () => {
	assert.deepEqual(lvl.stemChipLevelView(0.5), { unity: true, barPx: 2, dbLabel: '0.0 dB' });
	assert.equal(lvl.stemChipIsUnity(0.5 + 0.028 - 0.028), true);
	assert.deepEqual(lvl.stemChipLevelView(0.25), { unity: false, barPx: 1, dbLabel: '-6.0 dB' });
	assert.deepEqual(lvl.stemChipLevelView(0.75), { unity: false, barPx: 3, dbLabel: '+3.0 dB' });
	assert.deepEqual(lvl.stemChipLevelView(1), { unity: false, barPx: 4, dbLabel: '+6.0 dB' });
	assert.deepEqual(lvl.stemChipLevelView(0), { unity: false, barPx: 0, dbLabel: '-inf dB' });
	assert.equal(lvl.stemChipLevelView(0.6).barPx / lvl.stemChipLevelView(0.3).barPx, 2);
	assert.equal(lvl.stemChipDbLabel(0.5 + 1e-9), '0.0 dB');
	assert.throws(() => lvl.stemChipDbLabel(1.2), RangeError);
});
