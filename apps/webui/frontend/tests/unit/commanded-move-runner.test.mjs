import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

/** @type {import('./fixtures/commanded-move-runner-entry.ts')} */
let runner;
/** @type {import('./fixtures/commanded-move-runner-entry.ts')} */
let knobs;
let dialAnimations;

let t;
/** @type {Array<() => void>} */
let pending;

before(async () => {
	const listeners = new Map();
	const store = new Map();
	globalThis.window = {
		addEventListener: (type, fn) => listeners.set(type, fn),
		removeEventListener: (type) => listeners.delete(type),
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	globalThis.HTMLElement = class FakeHTMLElement {
		constructor(tagName, { isContentEditable = false } = {}) {
			this.tagName = tagName;
			this.isContentEditable = isContentEditable;
		}
	};
	({ runner, knobs, dialAnimations } = await loadTypeScriptModule(
		'tests/unit/fixtures/commanded-move-runner-entry.ts'
	));
});

after(() => {
	delete globalThis.window;
	delete globalThis.HTMLElement;
});

beforeEach(() => {
	knobs._resetKnobRegistryForTests();
	runner._resetCommandedMovesForTests();
	t = 1000;
	pending = [];
	runner._setCommandedMoveClockForTests({
		now: () => t,
		nextFrame: () =>
			new Promise((resolve) => {
				pending.push(resolve);
			})
	});
});

async function step(ms) {
	t += ms;
	const rs = pending;
	pending = [];
	for (const r of rs) r();
	await new Promise((resolve) => setImmediate(resolve));
}

function linearPlan(from, to, startMs, endMs) {
	return {
		startMs,
		endMs,
		valueAt(tm) {
			const span = endMs - startMs;
			if (span <= 0) return to;
			const u = Math.max(0, Math.min(1, (tm - startMs) / span));
			return from + (to - from) * u;
		}
	};
}

const BEAT_MS = 60000 / 124;

function registerTrackedDial(id, start) {
	const writes = [];
	let v = start;
	knobs.registerKnob({
		id,
		getValue: () => v,
		setValue: (x) => {
			v = x;
			writes.push({ t, x });
		}
	});
	return {
		writes,
		get value() {
			return v;
		},
		set value(next) {
			v = next;
		}
	};
}

test('instrument: runner writes through the shared bundle and lights the dot', async () => {
	const dial = registerTrackedDial('1:low', 1);
	const startMs = t;
	const endMs = startMs + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from) => linearPlan(from, 0, startMs, endMs));
	assert.ok(handle);
	for (let i = 0; i < 3; i += 1) await step(16);
	assert.ok(dial.value < 1);
	assert.equal(dial.writes.length, 3);
	assert.ok(dialAnimations.running['1:low']);
});

test('4-beat move to 0.0 under 16 ms steps lands exactly on zero at endMs', async () => {
	const dial = registerTrackedDial('1:low', 1);
	const t0 = t;
	const endMs = t0 + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	assert.ok(handle);
	while (pending.length > 0 || dial.value > 0) {
		await step(16);
		if (dial.writes.length > 500) break;
	}
	const result = await handle.done;
	assert.equal(result.outcome, 'completed');
	const last = dial.writes.at(-1);
	assert.ok(last !== undefined);
	assert.ok(Object.is(last.x, 0));
	const k = Math.ceil((endMs - t0) / 16);
	const firstZero = dial.writes.find((w) => Object.is(w.x, 0));
	assert.ok(firstZero);
	assert.equal(firstZero.t, t0 + 16 * k);
	for (const w of dial.writes) {
		if (w.t < firstZero.t) assert.ok(w.x > 0);
		if (w.t > firstZero.t) assert.fail('wrote past the final zero');
	}
	assert.equal(dialAnimations.running['1:low'], undefined);
});

test('drag mid-move stops runner writes', async () => {
	const dial = registerTrackedDial('1:low', 1);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	for (let i = 0; i < 3; i += 1) await step(16);
	knobs.beginSelectionDrag('1:low');
	const v = dial.value;
	dial.value = 0.9;
	knobs.setKnobFromDrag('1:low', v, 0.9, null, null);
	const n = dial.writes.length;
	for (let i = 0; i < 10; i += 1) await step(16);
	assert.equal(dial.writes.length, n);
	assert.equal(dial.value, 0.9);
	const result = await handle.done;
	assert.equal(result.outcome, 'cancelled');
	assert.equal(result.reason, 'moved by hand');
	assert.equal(dialAnimations.running['1:low'], undefined);
});

test('nudgeKnobOrSelection mid-move stops runner writes', async () => {
	const dial = registerTrackedDial('1:low', 1);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	for (let i = 0; i < 3; i += 1) await step(16);
	knobs.nudgeKnobOrSelection('1:low', 0.05);
	const n = dial.writes.length;
	for (let i = 0; i < 10; i += 1) await step(16);
	assert.equal(dial.writes.length, n);
	const result = await handle.done;
	assert.equal(result.outcome, 'cancelled');
	assert.equal(result.reason, 'moved by hand');
});

test('selection wheel mid-move stops runner writes', async () => {
	registerTrackedDial('1:low', 1);
	registerTrackedDial('2:low', 0.5);
	knobs.setKnobSelection(['1:low', '2:low']);
	const writes = [];
	let dial1 = 1;
	knobs.registerKnob({
		id: '1:low',
		getValue: () => dial1,
		setValue: (x) => {
			dial1 = x;
			writes.push({ t, id: '1:low', x });
		}
	});
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	for (let i = 0; i < 3; i += 1) await step(16);
	knobs.nudgeSelection(0.02);
	const n = writes.length;
	for (let i = 0; i < 10; i += 1) await step(16);
	assert.equal(writes.length, n);
	const result = await handle.done;
	assert.equal(result.outcome, 'cancelled');
});

test('second digit mid-move starts from the current value', async () => {
	const dial = registerTrackedDial('1:low', 1);
	const endA = t + 4 * BEAT_MS;
	const handleA = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endA));
	for (let i = 0; i < 5; i += 1) await step(16);
	const vMid = dial.value;
	assert.ok(vMid > 0 && vMid < 1);
	let fromRecorded = null;
	const endB = t + 4 * BEAT_MS;
	const writesBeforeB = dial.writes.length;
	const handleB = runner.startCommandedMove('1:low', (from, now) => {
		fromRecorded = from;
		return linearPlan(from, 1, now, endB);
	});
	assert.ok(handleB);
	assert.equal(fromRecorded, vMid);
	await step(16);
	const bWrite = dial.writes.slice(writesBeforeB)[0];
	assert.ok(bWrite);
	assert.ok(bWrite.x >= vMid && bWrite.x <= 1);
	const resultA = await handleA.done;
	assert.equal(resultA.outcome, 'replaced');
	const afterFirstBFrame = dial.writes.length;
	for (let i = 0; i < 20; i += 1) await step(16);
	assert.ok(dial.writes.length > afterFirstBFrame);
	assert.deepEqual(runner.activeCommandedMoveIds(), ['1:low']);
});

test('hidden tab jump writes the final value on the next frame', async () => {
	const dial = registerTrackedDial('1:low', 1);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	await step(16);
	await step(16);
	const n = dial.writes.length;
	await step(10_000);
	assert.equal(dial.writes.length, n + 1);
	assert.ok(Object.is(dial.writes.at(-1).x, 0));
	const result = await handle.done;
	assert.equal(result.outcome, 'completed');
	assert.equal(pending.length, 0);
});

test('every move end is logged with its outcome and reason', async () => {
	const dial = registerTrackedDial('1:low', 1);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	while (pending.length > 0) await step(16);
	await handle.done;
	const log = runner.commandedMoveLog();
	assert.equal(log.length, 1);
	assert.equal(log[0].control_id, '1:low');
	assert.equal(log[0].planned_end_ms, endMs);
	assert.ok(Number.isFinite(log[0].actual_end_ms));
	assert.equal(log[0].outcome, 'completed');
	assert.equal(log[0].reason, 'completed');
	runner._resetCommandedMovesForTests();
	t = 1000;
	pending = [];
	runner._setCommandedMoveClockForTests({
		now: () => t,
		nextFrame: () =>
			new Promise((resolve) => {
				pending.push(resolve);
			})
	});
	const handle2 = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	await step(16);
	knobs.nudgeKnobOrSelection('1:low', 0.1);
	await handle2.done;
	const cancelLog = runner.commandedMoveLog();
	assert.equal(cancelLog.length, 1);
	assert.equal(cancelLog[0].outcome, 'cancelled');
	assert.equal(cancelLog[0].reason, 'hand');
	void dial;
});

test('drag cancel logs reason hand', async () => {
	const dial = registerTrackedDial('1:low', 1);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	for (let i = 0; i < 3; i += 1) await step(16);
	knobs.beginSelectionDrag('1:low');
	const v = dial.value;
	dial.value = 0.9;
	knobs.setKnobFromDrag('1:low', v, 0.9, null, null);
	assert.equal(runner.commandedMoveLog().at(-1)?.reason, 'hand');
	await handle.done;
});

test('noteKnobHandInput with source midi cancels with reason midi in the same tick', async () => {
	const dial = registerTrackedDial('2:low', 1);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('2:low', (from, now) => linearPlan(from, 0, now, endMs));
	await step(16);
	const n = dial.writes.length;
	knobs.noteKnobHandInput('2:low', 'midi');
	const rec = runner.commandedMoveLog().at(-1);
	assert.deepEqual(
		{ control_id: rec.control_id, outcome: rec.outcome, reason: rec.reason },
		{ control_id: '2:low', outcome: 'cancelled', reason: 'midi' }
	);
	const result = await handle.done;
	assert.equal(result.outcome, 'cancelled');
	assert.equal(result.reason, 'moved by MIDI controller');
	for (let i = 0; i < 3; i += 1) await step(16);
	assert.equal(dial.writes.length, n);
});

test('midi on the linked partner cancels the move', async () => {
	registerTrackedDial('1:low', 0.5);
	registerTrackedDial('2:low', 0.5);
	knobs.altClickKnob('1:low');
	knobs.altClickKnob('2:low');
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0.9, now, endMs));
	await step(16);
	knobs.noteKnobHandInput('2:low', 'midi');
	const result = await handle.done;
	assert.equal(result.outcome, 'cancelled');
	assert.equal(result.reason, 'moved by MIDI controller');
	assert.equal(runner.commandedMoveLog().at(-1)?.reason, 'midi');
});

test('replacement logs reason replaced', async () => {
	const dial = registerTrackedDial('1:low', 1);
	const endA = t + 4 * BEAT_MS;
	const handleA = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endA));
	for (let i = 0; i < 5; i += 1) await step(16);
	const endB = t + 4 * BEAT_MS;
	const handleB = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 1, now, endB));
	assert.ok(handleB);
	const replaced = runner.commandedMoveLog().find((r) => r.control_id === '1:low' && r.outcome === 'replaced');
	assert.ok(replaced);
	assert.equal(replaced.reason, 'replaced');
	await handleA.done;
	void dial;
});

test('linked partner ends at applyLinkedDelta target without stagger accumulation (rising)', async () => {
	const startA = 0.5;
	const startB = 0.5;
	registerTrackedDial('1:low', startA);
	registerTrackedDial('2:low', startB);
	knobs.altClickKnob('1:low');
	knobs.altClickKnob('2:low');
	const target = 0.8;
	const endMs = t + 500;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, target, now, endMs));
	while (pending.length > 0) await step(16);
	const result = await handle.done;
	assert.equal(result.outcome, 'completed');
	const expected = knobs.applyLinkedDelta(startA, startB, target - startA);
	assert.equal(knobs.readKnobValue('1:low'), expected.primary);
	assert.equal(knobs.readKnobValue('2:low'), expected.secondary);
});

test('linked partner ends at applyLinkedDelta target without stagger accumulation (falling)', async () => {
	const startA = 0.8;
	const startB = 0.3;
	registerTrackedDial('1:low', startA);
	registerTrackedDial('2:low', startB);
	knobs.altClickKnob('1:low');
	knobs.altClickKnob('2:low');
	const target = 0.2;
	const endMs = t + 500;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, target, now, endMs));
	while (pending.length > 0) await step(16);
	const result = await handle.done;
	assert.equal(result.outcome, 'completed');
	const expected = knobs.applyLinkedDelta(startA, startB, target - startA);
	assert.equal(knobs.readKnobValue('1:low'), expected.primary);
	assert.equal(knobs.readKnobValue('2:low'), expected.secondary);
});

test('a dial whose committed value lags a frame is not cancelled', async () => {
	let committed = 1;
	let reported = 1;
	const writes = [];
	knobs.registerKnob({
		id: '1:low',
		getValue: () => reported,
		setValue: (x) => {
			committed = x;
			writes.push(x);
		}
	});
	const origStep = step;
	const lagStep = async (ms) => {
		reported = committed;
		await origStep(ms);
	};
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	while (pending.length > 0 || committed > 0) {
		await lagStep(16);
		if (writes.length > 500) break;
	}
	const result = await handle.done;
	assert.equal(result.outcome, 'completed');
	assert.ok(Object.is(writes.at(-1), 0));
});

test('unmount mid-move cancels', async () => {
	registerTrackedDial('1:low', 1);
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0, now, endMs));
	await step(16);
	knobs.unregisterKnob('1:low');
	await step(16);
	const result = await handle.done;
	assert.equal(result.outcome, 'cancelled');
	assert.equal(result.reason, 'control unmounted');
});

test('hand nudge on the linked partner cancels the move', async () => {
	registerTrackedDial('1:low', 0.5);
	registerTrackedDial('2:low', 0.5);
	knobs.altClickKnob('1:low');
	knobs.altClickKnob('2:low');
	const endMs = t + 4 * BEAT_MS;
	const handle = runner.startCommandedMove('1:low', (from, now) => linearPlan(from, 0.9, now, endMs));
	await step(16);
	knobs.nudgeKnobOrSelection('2:low', 0.01);
	const result = await handle.done;
	assert.equal(result.outcome, 'cancelled');
	assert.equal(result.reason, 'moved by hand');
});

test('startCommandedMove returns null when the control is not mounted', () => {
	assert.equal(runner.startCommandedMove('9:low', () => linearPlan(0, 1, 0, 1)), null);
});

test('invalid plan window throws RangeError', () => {
	registerTrackedDial('1:low', 0.5);
	assert.throws(
		() =>
			runner.startCommandedMove('1:low', () => ({
				startMs: 10,
				endMs: 5,
				valueAt: () => 0
			})),
		RangeError
	);
});

test('non-finite valueAt rejects done', async () => {
	registerTrackedDial('1:low', 0.5);
	const handle = runner.startCommandedMove('1:low', (_, now) => ({
		startMs: now,
		endMs: now + 100,
		valueAt: () => NaN
	}));
	const rejected = assert.rejects(handle.done, RangeError);
	await step(16);
	await rejected;
});

function functionBody(source, name) {
	const start = source.indexOf(`function ${name}(`);
	assert.ok(start >= 0, `missing function ${name}`);
	const tail = source.slice(start);
	const end = tail.search(/\n\tfunction |\n\t\$effect/);
	return end < 0 ? tail : tail.slice(0, end);
}

test('VFader reports hand input on wheel, arrow and single-fader drag paths', () => {
	const source = readFileSync(join(FE_ROOT, 'src/lib/components/rb/mixer/VFader.svelte'), 'utf8');
	for (const fn of ['handleKeyDown', 'handleWheelAdjust', 'handlePointerMove']) {
		const body = functionBody(source, fn);
		assert.match(body, /noteKnobHandInput\(controlId\)/, `${fn} must call noteKnobHandInput`);
	}
});

test('runner source uses engine clock seam only (no Date.now or requestAnimationFrame)', () => {
	const source = readFileSync(
		join(FE_ROOT, 'src/lib/rb/commanded-move-runner.svelte.ts'),
		'utf8'
	);
	assert.doesNotMatch(source, /\bDate\.now\b/);
	assert.doesNotMatch(source, /\brequestAnimationFrame\b/);
});
