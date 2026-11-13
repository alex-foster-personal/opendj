/**
 * Deck grid adjust / grid shift (GRIDEDIT-6235, GRIDEDIT-6235d, GRIDEDIT-10215): what a press asks
 * for, how the edit in force reads, and the per-track request queue.
 *
 * Regression lines:
 * - if the right half of grid adjust moves the grid earlier then broken
 * - if Shift does not make the adjust step coarse then broken
 * - if Alt-click nudges instead of resetting then broken
 * - if a keyboard click (no pointer position) does nothing then broken
 * - if grid shift ever asks for a time offset then broken (whole beats only)
 * - if a held button sends one request per repeat while one is in flight then
 *   the engine is flooded: broken (nudges merge)
 * - if merged nudges do not sum exactly then the grid lands off the presses
 * - if a reset runs before a nudge pressed ahead of it then broken (ordered)
 * - if a failed request leaves the rest of the queue to replay then broken
 * - if a failed press's promise resolves (or a dropped one never settles) then
 *   the dispatcher reports success for an edit that never landed: broken
 * - if the grid buttons call the queue directly instead of the `grid_edit`
 *   performance command then a preset's lifecycle lock never sees them: broken
 * - if `grid_edit` accepts an edit the engine would refuse (fractional beats,
 *   an unbounded offset, an unknown kind) then the IPC contract is open: broken
 * - if the deck re-reads the grid once per press instead of once per drain then broken
 * - overshoot control: if the queue claims the event echo forever then an
 *   agent's later edit never reaches the deck: broken
 * - if the deck still renders the grid buttons inert with a plannedTitle then broken
 * - if Cmd+Z on a focused grid button does not send undo then broken
 * - if plain z sends undo then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let edit;
let command;
let queueMod;
let explainers;

before(async () => {
	edit = await loadTypeScriptModule('src/lib/rb/grid-edit.ts');
	command = await loadTypeScriptModule('src/lib/rb/grid-edit-command.ts');
	queueMod = await loadTypeScriptModule('src/lib/rb/grid-edit-queue.ts');
	explainers = await loadTypeScriptModule('src/lib/rb/planned-explainers.ts');
});

const press = (offsetX, overrides = {}) => ({
	offsetX,
	width: 20,
	shiftKey: false,
	altKey: false,
	...overrides
});

function anlzWith(beats, editValue) {
	return {
		stable_id: 'a'.repeat(40),
		beatgrid: {
			source: 'rekordbox',
			beat_count: beats.length,
			beats,
			...(editValue === undefined ? {} : { edit: editValue })
		},
		beatgrid_source: 'rekordbox'
	};
}

const BEATS = [
	{ n: 1, bpm: 120, t: 0.5 },
	{ n: 2, bpm: 120, t: 1.0 }
];

test('grid adjust: right half later, left half earlier, Shift coarse', () => {
	assert.deepEqual(edit.intentForPress('adjust', press(15)), { kind: 'nudge', offset_ms: 2, beats: 0 });
	assert.deepEqual(edit.intentForPress('adjust', press(4)), { kind: 'nudge', offset_ms: -2, beats: 0 });
	assert.deepEqual(edit.intentForPress('adjust', press(15, { shiftKey: true })), {
		kind: 'nudge',
		offset_ms: edit.GRID_ADJUST_COARSE_STEP_MS,
		beats: 0
	});
	assert.equal(edit.GRID_ADJUST_STEP_MS, 2);
	assert.equal(edit.GRID_ADJUST_COARSE_STEP_MS, 10);
});

test('grid shift: whole beats only, never a time offset', () => {
	assert.deepEqual(edit.intentForPress('shift', press(15)), { kind: 'nudge', offset_ms: 0, beats: 1 });
	assert.deepEqual(edit.intentForPress('shift', press(4)), { kind: 'nudge', offset_ms: 0, beats: -1 });
	assert.deepEqual(edit.intentForPress('shift', press(15, { shiftKey: true })), {
		kind: 'nudge',
		offset_ms: 0,
		beats: 1
	});
});

test('Alt-click resets that half; a keyboard click (no position) steps later', () => {
	assert.deepEqual(edit.intentForPress('adjust', press(4, { altKey: true })), { kind: 'reset', part: 'adjust' });
	assert.deepEqual(edit.intentForPress('shift', press(15, { altKey: true })), { kind: 'reset', part: 'shift' });
	assert.deepEqual(edit.intentForPress('adjust', press(null)), { kind: 'nudge', offset_ms: 2, beats: 0 });
	assert.equal(edit.directionFromPress(10, 0), 1, 'a zero-width box is not read as a left-half press');
});

test('arrow keys step earlier and later; other keys are not handled', () => {
	assert.deepEqual(edit.intentForKey('adjust', 'ArrowLeft', false), { kind: 'nudge', offset_ms: -2, beats: 0 });
	assert.deepEqual(edit.intentForKey('adjust', 'ArrowRight', true), { kind: 'nudge', offset_ms: 10, beats: 0 });
	assert.deepEqual(edit.intentForKey('shift', 'ArrowLeft', false), { kind: 'nudge', offset_ms: 0, beats: -1 });
	assert.equal(edit.intentForKey('shift', 'Enter', false), null);
});

test('offset and shift read as words, and merged steps stay exact', () => {
	assert.equal(edit.formatOffsetMs(6), '+6 ms');
	assert.equal(edit.formatOffsetMs(-2.5), '-2.5 ms');
	assert.equal(edit.formatOffsetMs(-0), '0 ms');
	assert.equal(edit.formatBeatShift(0), 'measured bar 1');
	assert.equal(edit.formatBeatShift(1), 'bar 1 one beat later');
	assert.equal(edit.formatBeatShift(2), 'bar 1 two beats later');
	assert.equal(edit.formatBeatShift(-1), 'bar 1 one beat earlier');
	let sum = { offset_ms: 0, beats: 0 };
	for (let i = 0; i < 10; i += 1) sum = edit.mergeNudges(sum, { offset_ms: 0.1, beats: 0 });
	assert.equal(sum.offset_ms, 1);
});

test('titles say what the button does, how to drive it, and the edit in force', () => {
	const unedited = anlzWith(BEATS);
	assert.match(edit.gridAdjustTitle(unedited, null), /^Grid adjust - nudges the beatgrid/);
	assert.match(edit.gridAdjustTitle(unedited, null), /Now: 0 ms on the rekordbox grid/);
	const edited = anlzWith(BEATS, { source: 'rekordbox', offset_ms: 6, beat_shift: 1 });
	assert.match(edit.gridAdjustTitle(edited, null), /Now: \+6 ms on the rekordbox grid/);
	assert.match(edit.gridShiftTitle(edited, null), /Now: bar 1 one beat later on the rekordbox grid/);
	assert.equal(edit.gridEditPartLit(edited, 'adjust'), true);
	assert.equal(edit.gridEditPartLit(unedited, 'shift'), false);
	assert.doesNotMatch(edit.gridAdjustTitle(edited, null), /Not built yet/);
});

test('a gridless, stick or empty deck names why it cannot be edited', () => {
	assert.equal(edit.gridEditUnavailableReason(null, null, false), 'no track is loaded on this deck');
	assert.match(edit.gridEditUnavailableReason('usb:x', anlzWith(BEATS), true), /USB stick/);
	assert.match(edit.gridEditUnavailableReason('a', null, false), /not loaded yet/);
	assert.match(edit.gridEditUnavailableReason('a', anlzWith([]), false), /no beatgrid/);
	assert.equal(edit.gridEditUnavailableReason('a', anlzWith(BEATS), false), null);
	// Codex P2 on PR #6235: a /beatgrid-fallback grid never applies an edit.
	assert.match(edit.gridEditUnavailableReason('a', (() => { const a = anlzWith(BEATS); return { ...a, beatgrid: { ...a.beatgrid, fallback: true } }; })(), false), /provisional estimate/);
	assert.match(edit.gridShiftTitle(null, 'no track is loaded on this deck'), /Unavailable: no track is loaded/);
});

test('a locked grid disables both deck grid buttons with the GRID_LOCKED reason (GRIDEDIT-9763)', () => {
	const base = anlzWith(BEATS);
	const locked = { ...base, beatgrid: { ...base.beatgrid, locked: true } };
	const reason = edit.gridEditUnavailableReason('a', locked, false);
	assert.equal(reason, edit.GRID_LOCKED_REASON);
	assert.match(reason, /GRID_LOCKED/);
	assert.match(edit.gridAdjustTitle(locked, reason), /Unavailable: the beatgrid is locked/);
	assert.match(edit.gridShiftTitle(locked, reason), /Unavailable: the beatgrid is locked/);
	assert.equal(edit.gridEditUnavailableReason('a', base, false), null, 'an unlocked grid stays editable');
});

test('a mouse press on the left half nudges earlier: pointerdown detail 0 is not a keyboard press', () => {
	// Pointer Events spec: pointerdown carries detail 0, so it must still use its position.
	assert.equal(edit.pressOffsetX('pointerdown', 0, 104, 100), 4);
	const left = { offsetX: edit.pressOffsetX('pointerdown', 0, 104, 100), width: 20, shiftKey: false, altKey: false };
	assert.deepEqual(edit.intentForPress('adjust', left), { kind: 'nudge', offset_ms: -2, beats: 0 });
	// A mouse click (detail >= 1) keeps its position; a keyboard click (detail 0) has none.
	assert.equal(edit.pressOffsetX('click', 1, 104, 100), 4);
	assert.equal(edit.pressOffsetX('click', 0, 0, 100), null);
	const buttons = readFileSync(
		new URL('../../src/lib/components/rb/deck/GridEditButtons.svelte', import.meta.url),
		'utf8'
	);
	assert.match(buttons, /offsetX: pressOffsetX\(event\.type, event\.detail, event\.clientX, rect\.left\)/);
});

test('the deck mounts the live buttons and the planned entries are gone', () => {
	const deck = readFileSync(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url), 'utf8');
	assert.match(deck, /<GridEditButtons\s+\{deck\}\s+\{deckId\}\s+press=\{\(edit\) => void runPerformanceCommandFromUi\(\{ type: 'grid_edit', deck: deckId, edit \}\)\}\s+reportError=\{\(message: string\) => pushToast\(message, 'error'\)\}\s+\/>/);
	assert.doesNotMatch(deck, /plannedTitle\('grid-(adjust|shift)'\)/);
	assert.equal('grid-adjust' in explainers.PLANNED_CONTROLS, false);
	assert.equal('grid-shift' in explainers.PLANNED_CONTROLS, false);
	const buttons = readFileSync(
		new URL('../../src/lib/components/rb/deck/GridEditButtons.svelte', import.meta.url),
		'utf8'
	);
	assert.doesNotMatch(buttons, /rb-inert/);
	assert.match(buttons, /title=\{adjustTitle\}/);
	assert.match(buttons, /title=\{shiftTitle\}/);
});

// ------------------------------------------------------------------ the queue

function deferred() {
	let resolve;
	let reject;
	const promise = new Promise((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

function fakePorts() {
	const calls = [];
	const errors = [];
	const gates = [];
	let stored = { source: 'rekordbox', offset_ms: 0, beat_shift: 0 };
	const gate = () => {
		const d = deferred();
		gates.push(d);
		return d.promise;
	};
	const ports = {
		async nudge(id, source, step) {
			calls.push(['nudge', id, source, step]);
			await gate();
			stored = { ...stored, offset_ms: stored.offset_ms + step.offset_ms, beat_shift: stored.beat_shift + step.beats };
			return stored;
		},
		async reset(id, source, part) {
			calls.push(['reset', id, source, part]);
			stored = part === 'adjust' ? { ...stored, offset_ms: 0 } : { ...stored, beat_shift: 0 };
			return stored;
		},
		async undo(id, source) {
			calls.push(['undo', id, source]);
			stored = { offset_ms: 0, beat_shift: 0 };
			return stored;
		},
		async refresh(id) {
			calls.push(['refresh', id]);
		}
	};
	return {
		ports,
		calls,
		errors,
		gates,
		stored: () => stored
	};
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const SID = 'b'.repeat(40);

/** Let every gated request succeed, in order, until the track's queue is idle. */
async function settle(fake, q) {
	for (let round = 0; round < 50 && q.busy(SID); round += 1) {
		await tick();
		while (fake.gates.length > 0) fake.gates.shift().resolve();
	}
	await q.idle(SID);
}

test('presses during an in-flight nudge merge into one request, then one refresh', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	await tick();
	for (let i = 0; i < 5; i += 1) q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	assert.equal(q.busy(SID), true);
	await settle(fake, q);
	assert.deepEqual(
		fake.calls.map((call) => call[0]),
		['nudge', 'nudge', 'refresh'],
		'six presses, two requests, one re-read'
	);
	assert.deepEqual(fake.calls[1][3], { offset_ms: 10, beats: 0 });
	assert.equal(fake.stored().offset_ms, 12);
	assert.equal(q.busy(SID), false);
});

// Codex P2 on PR #6235: the backend refuses any one nudge request past
// +/-500 ms or +/-64 beats, so a merged run of held presses must split at
// that bound instead of failing whole and landing none of them.
test('merged presses split at the API step bound, never past it', async () => {
	assert.deepEqual(edit.mergeNudges({ offset_ms: 490, beats: 0 }, { offset_ms: 10, beats: 0 }), { offset_ms: 500, beats: 0 });
	assert.equal(edit.mergeNudges({ offset_ms: 500, beats: 0 }, { offset_ms: 10, beats: 0 }), null);
	assert.equal(edit.mergeNudges({ offset_ms: -500, beats: 0 }, { offset_ms: -0.5, beats: 0 }), null);
	assert.equal(edit.mergeNudges({ offset_ms: 0, beats: 63 }, { offset_ms: 0, beats: 3 }), null);
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 10, beats: 0 });
	await tick();
	for (let i = 0; i < 60; i += 1) q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 10, beats: 0 });
	await settle(fake, q);
	const steps = fake.calls.filter((call) => call[0] === 'nudge').map((call) => call[3].offset_ms);
	assert.deepEqual(steps, [10, 500, 100], 'the in-flight press, one full step, the rest');
	assert.equal(fake.stored().offset_ms, 610, 'every press landed');
});

test('the dispatcher coalescer opens a new merge window at the API step bound', async () => {
	const { createGridNudgeCoalescer } = await loadTypeScriptModule('src/lib/rb/grid-edit-command.ts');
	const c = createGridNudgeCoalescer(() => null);
	const nudge = (ms) => ({ deck: 1, edit: { kind: 'nudge', offset_ms: ms, beats: 0 } });
	const first = nudge(10);
	const settled = Promise.resolve('first');
	c.queued(first, settled);
	for (let i = 0; i < 49; i += 1) assert.equal(c.merge(nudge(10), () => true), settled);
	assert.equal(first.edit.offset_ms, 500);
	const over = nudge(10);
	assert.equal(c.merge(over, () => true), null, 'past the bound it queues as its own command');
	assert.equal(first.edit.offset_ms, 500, 'and the queued request is left within the bound');
	c.queued(over, Promise.resolve('second'));
	assert.notEqual(c.merge(nudge(10), () => true), null, 'control: later presses merge into the new one');
	assert.equal(over.edit.offset_ms, 20);
});

test('a reset runs after the nudge pressed before it and keeps the other half', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 0, beats: 1 });
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 4, beats: 0 });
	q.submit(SID, 'rekordbox', { kind: 'reset', part: 'adjust' });
	await settle(fake, q);
	assert.deepEqual(
		fake.calls.map((call) => call[0]),
		['nudge', 'nudge', 'reset', 'refresh']
	);
	assert.deepEqual(fake.calls[1][3], { offset_ms: 4, beats: 0 }, 'the first nudge was in flight, so no merge');
	assert.equal(fake.calls[2][3], 'adjust', 'one atomic server-side reset of one half, no client read-then-write');
	assert.deepEqual(fake.stored(), { source: 'rekordbox', offset_ms: 0, beat_shift: 1 });
});

// Codex P2 on PR #6235: the server saturates each step at the stored limit,
// so presses in opposite directions keep their order instead of summing.
test('presses in opposite directions go as separate steps, in order', async () => {
	assert.equal(edit.mergeNudges({ offset_ms: 20, beats: 0 }, { offset_ms: -10, beats: 0 }), null);
	assert.deepEqual(edit.mergeNudges({ offset_ms: 2, beats: 0 }, { offset_ms: 0, beats: 1 }), { offset_ms: 2, beats: 1 });
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 0, beats: 1 });
	await tick();
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: -2, beats: 0 });
	await settle(fake, q);
	const steps = fake.calls.filter((call) => call[0] === 'nudge').map((call) => call[3].offset_ms);
	assert.deepEqual(steps, [0, 4, -2], 'same-direction presses still merge; a reversal starts a new step');
	assert.equal(fake.calls.at(-1)[0], 'refresh');
});

test('merged presses that cancel out write nothing and re-read nothing', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 0, beats: 1 });
	await tick();
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 0, beats: 1 });
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 0, beats: -1 });
	await settle(fake, q);
	assert.deepEqual(fake.calls.map((call) => call[0]), ['nudge', 'refresh']);
});

// Codex P2 on PR #6235: two decks hold one track, so deck B's press can wait
// behind deck A's write while deck B loads another track; the press must be
// re-checked right before its own write.
test('a guarded press refused while it waits writes nothing and drops nothing else', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	let deckBChanged = false;
	const guard = () => {
		if (deckBChanged) throw new Error('grid edit: deck 2 loaded another track before the edit ran; nothing was written');
	};
	const first = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 }, () => {});
	await tick();
	const stale = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 4, beats: 0 }, guard);
	const staleRefused = assert.rejects(stale, /deck 2 loaded another track before the edit ran; nothing was written/);
	const after = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 8, beats: 0 }, () => {});
	deckBChanged = true;
	await settle(fake, q);
	await first;
	await staleRefused;
	await after;
	assert.deepEqual(
		fake.calls.filter((call) => call[0] === 'nudge').map((call) => call[3].offset_ms),
		[2, 8],
		'the refused press wrote nothing, was not merged, and the press after it still ran'
	);
	// Control: an unchanged deck's guarded press writes.
	const kept = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 1, beats: 0 }, () => {});
	await settle(fake, q);
	await kept;
	assert.equal(fake.calls.filter((call) => call[0] === 'nudge').at(-1)[3].offset_ms, 1);
});

test('the dispatcher re-checks a grid edit target right before the write', () => {
	const ipc = readFileSync(new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url), 'utf8');
	assert.match(ipc, /await runGridEditCommand\(st\.stable_id, st\.anlz, command\.edit, \(\) =>\n\t\t\t_assertGridEditTarget\(command, getDeckState\(command\.deck\)\)\n\t\t\);/);
	const api = readFileSync(new URL('../../src/lib/rb/grid-edit-api.ts', import.meta.url), 'utf8');
	assert.match(api, /submit\(stableId, anlz\.beatgrid\.source, intent, guard\)/);
});

test('a failed request rejects its press, and the rest of the queue is dropped with the same error', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	const first = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	await tick();
	const second = q.submit(SID, 'rekordbox', { kind: 'reset', part: 'shift' });
	fake.gates.shift().reject(new Error('GRID_EDIT_OUT_OF_RANGE'));
	const outcomes = await Promise.allSettled([first, second]);
	await settle(fake, q);
	assert.deepEqual(fake.calls.map((call) => call[0]), ['nudge']);
	assert.deepEqual(outcomes.map((o) => o.status), ['rejected', 'rejected']);
	assert.equal(outcomes[0].reason.message, 'Grid edit failed - GRID_EDIT_OUT_OF_RANGE');
	// One error object, so the dispatcher toasts it once, not once per press.
	assert.equal(outcomes[1].reason, outcomes[0].reason);
});

test('[if] a press succeeds [then] its promise resolves only after the re-read, [else stop]', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	let settled = false;
	const done = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 }).then(() => { settled = true; });
	await tick();
	assert.equal(settled, false, 'still in flight');
	await settle(fake, q);
	await done;
	assert.deepEqual(fake.calls.map((call) => call[0]), ['nudge', 'refresh']);
	assert.equal(settled, true);
});

test('[if] the re-read fails after a saved edit [then] the press rejects saying it was saved, [else stop]', async () => {
	const fake = fakePorts();
	fake.ports.refresh = async () => { throw new Error('503'); };
	const q = queueMod.createGridEditQueue(fake.ports);
	const press = q.submit(SID, 'rekordbox', { kind: 'reset', part: 'adjust' });
	await assert.rejects(press, { message: 'Grid edit saved, but the deck could not re-read the grid - 503' });
});

test('merged presses settle together with the one request they became', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	const a = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	await tick();
	const b = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	const c = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	await settle(fake, q);
	await Promise.all([a, b, c]);
	assert.deepEqual(fake.calls.filter((call) => call[0] === 'nudge').map((call) => call[3].offset_ms), [2, 4]);
});

test('grid_edit intents are validated like any IPC input', () => {
	assert.deepEqual(command.parseGridEditIntent({ kind: 'nudge', offset_ms: 2, beats: 0 }), { kind: 'nudge', offset_ms: 2, beats: 0 });
	assert.deepEqual(command.parseGridEditIntent({ kind: 'reset', part: 'shift' }), { kind: 'reset', part: 'shift' });
	assert.throws(() => command.parseGridEditIntent({ kind: 'nudge', offset_ms: 2, beats: 0.5 }), RangeError);
	assert.throws(() => command.parseGridEditIntent({ kind: 'nudge', offset_ms: 501, beats: 0 }), RangeError);
	assert.throws(() => command.parseGridEditIntent({ kind: 'nudge', offset_ms: Number.NaN, beats: 0 }), RangeError);
	assert.throws(() => command.parseGridEditIntent({ kind: 'nudge', offset_ms: 2, beats: 0, extra: 1 }), TypeError);
	assert.throws(() => command.parseGridEditIntent({ kind: 'reset', part: 'both' }), TypeError);
	assert.throws(() => command.parseGridEditIntent({ kind: 'set' }), TypeError);
	assert.throws(() => command.parseGridEditIntent(null), TypeError);
});

test('the grid buttons press through the grid_edit performance command, never the queue', () => {
	const buttons = readFileSync(new URL('../../src/lib/components/rb/deck/GridEditButtons.svelte', import.meta.url), 'utf8');
	assert.match(buttons, /sendEdit\(intent\);/);
	assert.doesNotMatch(buttons, /\.submit\(/);
	const deck = readFileSync(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url), 'utf8');
	assert.match(deck, /press=\{\(edit\) => void runPerformanceCommandFromUi\(\{ type: 'grid_edit', deck: deckId, edit \}\)\}/);
	const ipc = readFileSync(new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url), 'utf8');
	const types = readFileSync(new URL('../../src/lib/rb/performance-ipc-types.ts', import.meta.url), 'utf8');
	assert.match(types, /\| \{ type: 'grid_edit'; deck: DeckId; edit: GridEditIntent \}/);
	assert.match(ipc, /command\.type === 'grid_edit'\) \{\n\t\tconst st = getDeckState\(command\.deck\);\n\t\t_assertGridEditTarget\(command, st\);/);
	assert.match(ipc, /\t\tawait runGridEditCommand\(st\.stable_id, st\.anlz, command\.edit, \(\) =>/);
});

test('[if] an agent edit lands just after a local press settles [then] the deck re-reads it, [else stop]', async () => {
	// The old 1.5 s "own echo" window dropped exactly this: evidence capture
	// saw the server at +20 ms while the deck stayed at +30 ms.
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	const reads = [];
	const ev = queueMod.createEventRefresher(q, async (id) => reads.push(id), (m) => fake.errors.push(m));
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	await settle(fake, q);
	ev.onEvent(SID);
	await ev.idle(SID);
	assert.deepEqual(reads, [SID], 'an event right after our own drain is still honored');
	ev.onEvent(SID);
	await ev.idle(SID);
	assert.deepEqual(reads, [SID, SID], 'every later event re-reads too');
});

// Sol P1 on PR #6235: a drain made only of refused presses finishes without
// awaiting, and must not leave the queue marked running forever.
test('[if] a guarded press is refused on an idle queue [then] the next press still runs, [else stop]', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	const refused = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 }, () => {
		throw new Error('deck changed');
	});
	await assert.rejects(refused, /deck changed/);
	await tick();
	assert.equal(q.busy(SID), false, 'a drain that wrote nothing leaves the queue idle');
	const next = q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	await settle(fake, q);
	await next;
	assert.deepEqual(fake.calls.map((call) => call[0]), ['nudge', 'refresh'], 'the later press was written');
});

// Sol P2 on PR #6235: two decks holding the track are each handed the same event.
test('[if] one event reaches two decks on the same track [then] the track is re-read once, [else stop]', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	const reads = [];
	const ev = queueMod.createEventRefresher(q, async (id) => reads.push(id), () => {});
	ev.onEvent(SID);
	ev.onEvent(SID);
	await ev.idle(SID);
	assert.deepEqual(reads, [SID], 'an event before the re-read is sent is covered by it');
});

test('[if] an event arrives mid-drain [then] its re-read waits for the drain, [else stop]', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	const order = [];
	fake.ports.refresh = async (id) => order.push(['drain-refresh', id]);
	const ev = queueMod.createEventRefresher(q, async (id) => order.push(['event-refresh', id]), () => {});
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	await tick();
	ev.onEvent(SID);
	await tick();
	assert.deepEqual(order, [], 'nothing re-reads while the nudge is in flight');
	await settle(fake, q);
	await ev.idle(SID);
	assert.deepEqual(order.map((o) => o[0]), ['drain-refresh', 'event-refresh']);
});

test('[if] events pile up during one re-read [then] exactly one more re-read runs, [else stop]', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	const gates = [];
	let reads = 0;
	const ev = queueMod.createEventRefresher(
		q,
		async () => {
			reads += 1;
			const d = deferred();
			gates.push(d);
			await d.promise;
		},
		() => {}
	);
	ev.onEvent(SID);
	await tick();
	assert.equal(reads, 1);
	for (let i = 0; i < 4; i += 1) ev.onEvent(SID);
	gates.shift().resolve();
	await tick();
	await tick();
	assert.equal(reads, 2, 'four events during a re-read owe one more, since it may have read before their writes');
	gates.shift().resolve();
	await ev.idle(SID);
	assert.equal(reads, 2, 'and no more than one');
});

test('[if] an event re-read fails [then] it is reported and the next event still re-reads, [else stop]', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	const errors = [];
	let fail = true;
	let reads = 0;
	const ev = queueMod.createEventRefresher(
		q,
		async () => {
			reads += 1;
			if (fail) throw new Error('503');
		},
		(m) => errors.push(m)
	);
	ev.onEvent(SID);
	await ev.idle(SID);
	assert.deepEqual(errors, ['Grid was edited elsewhere, but this deck could not re-read it - 503']);
	fail = false;
	ev.onEvent(SID);
	await ev.idle(SID);
	assert.equal(reads, 2);
});

// Codex P1 on PR #6235: a load does not take the persistence scope, so a grid
// edit queued behind another one could run after the deck loaded a different
// track and move THAT track's grid.
test('[if] the deck loads another track while a grid edit waits [then] the edit refuses and writes nothing, [else stop]', async () => {
	const entry = await loadTypeScriptModule('tests/unit/fixtures/grid-edit-ipc-entry.ts', {
		viteApiBase: 'https://grid-edit-bind.example.test'
	});
	const A = 'a'.repeat(40);
	const B = 'c'.repeat(40);
	const originalFetch = globalThis.fetch;
	const originalWindow = globalThis.window;
	const nudged = [];
	const sources = [];
	let release;
	const held = new Promise((resolve) => { release = resolve; });
	let holdNext = null;
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		const match = /\/tracks\/([^/]+)\/grid-edit\/nudge$/.exec(new URL(request.url).pathname);
		if (request.method === 'POST' && match !== null) {
			nudged.push(match[1]);
			sources.push(JSON.parse(await request.text()).source);
			if (nudged.length === 1) await held;
			if (holdNext !== null) {
				const wait = holdNext;
				holdNext = null;
				await wait;
			}
			return new Response(JSON.stringify({ stable_id: match[1], source: 'rekordbox', offset_ms: 2, beat_shift: 0, modified_at: null }), {
				status: 200, headers: { 'content-type': 'application/json' }
			});
		}
		return new Response('{}', { status: 500, headers: { 'content-type': 'application/json' } });
	};
	globalThis.window = {};
	const uninstall = entry.installPerformanceBrowserIpc();
	const deck = entry.deckStates[1];
	const prior = { stable_id: deck.stable_id, load_generation: deck.load_generation, anlz: deck.anlz };
	const anlz = { beatgrid: { source: 'rekordbox', beats: [{ t: 0.5, n: 1, bpm: 120 }] } };
	try {
		Object.assign(deck, { stable_id: A, load_generation: 5, anlz });
		const press = { type: 'grid_edit', deck: 1, edit: { kind: 'nudge', offset_ms: 2, beats: 0 } };
		const first = entry.dispatchPerformanceCommand({ ...press }).catch((error) => error);
		await new Promise((resolve) => setTimeout(resolve, 10));
		const second = entry.dispatchPerformanceCommand({ ...press }).catch((error) => error);
		await new Promise((resolve) => setTimeout(resolve, 10));
		Object.assign(deck, { stable_id: B, load_generation: 6, anlz });
		release();
		await first;
		const refused = await second;
		assert.ok(refused instanceof Error, 'the edit pressed for A must not run on B');
		assert.match(refused.message, /loaded another track before the edit ran; nothing was written/);
		assert.deepEqual(nudged, [A], 'no request ever named B');
		// Control: a press made with B loaded edits B.
		await entry.dispatchPerformanceCommand({ ...press }).catch(() => {});
		assert.deepEqual(nudged, [A, B]);
		// Codex P2: an analysis-source switch does not take the persistence
		// scope either, so an edit pressed on the rekordbox grid that runs
		// after the deck switched to the own grid must not write the own edit.
		let releaseHeld;
		const heldAgain = new Promise((resolve) => { releaseHeld = resolve; });
		holdNext = heldAgain;
		const third = entry.dispatchPerformanceCommand({ ...press }).catch((error) => error);
		await new Promise((resolve) => setTimeout(resolve, 10));
		const fourth = entry.dispatchPerformanceCommand({ ...press }).catch((error) => error);
		await new Promise((resolve) => setTimeout(resolve, 10));
		Object.assign(deck, { anlz: { beatgrid: { source: 'own', beats: anlz.beatgrid.beats } } });
		releaseHeld();
		await third;
		const switched = await fourth;
		assert.ok(switched instanceof Error, 'the edit pressed on the rekordbox grid must not land on own');
		assert.match(switched.message, /switched beatgrid source before the edit ran; nothing was written/);
		assert.deepEqual(sources, ['rekordbox', 'rekordbox', 'rekordbox'], 'no request ever named own');
	} finally {
		Object.assign(deck, prior);
		uninstall();
		globalThis.fetch = originalFetch;
		globalThis.window = originalWindow;
	}
});

// Codex P2 on PR #6235: every grid_edit waits on the persistence scope while
// the one ahead writes and re-reads, so held-button repeats never reached the
// edit queue's merge; a slow backend wrote one nudge per 80 ms tick and kept
// writing a backlog after release.
test('[if] repeats arrive while a nudge writes [then] they merge into one queued command, a reset still cuts the merge, [else stop]', async () => {
	const entry = await loadTypeScriptModule('tests/unit/fixtures/grid-edit-ipc-entry.ts', {
		viteApiBase: 'https://grid-edit-coalesce.example.test'
	});
	const SID_C = 'd'.repeat(40);
	const originalFetch = globalThis.fetch;
	const originalWindow = globalThis.window;
	const writes = [];
	let release;
	const held = new Promise((resolve) => { release = resolve; });
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		const path = new URL(request.url).pathname;
		if (request.method === 'POST' && path.endsWith('/grid-edit/nudge')) {
			const body = JSON.parse(await request.text());
			writes.push(['nudge', body.offset_ms]);
			if (writes.length === 1) await held;
		} else if (request.method === 'DELETE' && path.endsWith('/grid-edit')) {
			writes.push(['reset', new URL(request.url).searchParams.get('part')]);
		} else {
			return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
		}
		return new Response(JSON.stringify({ stable_id: SID_C, source: 'rekordbox', offset_ms: 0, beat_shift: 0, modified_at: null }), {
			status: 200, headers: { 'content-type': 'application/json' }
		});
	};
	globalThis.window = {};
	const uninstall = entry.installPerformanceBrowserIpc();
	const deck = entry.deckStates[2];
	const prior = { stable_id: deck.stable_id, load_generation: deck.load_generation, anlz: deck.anlz };
	try {
		Object.assign(deck, { stable_id: SID_C, load_generation: 3, anlz: { beatgrid: { source: 'rekordbox', beats: [{ t: 0.5, n: 1, bpm: 120 }] } } });
		const nudge = { type: 'grid_edit', deck: 2, edit: { kind: 'nudge', offset_ms: 2, beats: 0 } };
		const presses = [entry.dispatchPerformanceCommand({ ...nudge, edit: { ...nudge.edit } })];
		await new Promise((resolve) => setTimeout(resolve, 10));
		for (let i = 0; i < 5; i += 1) presses.push(entry.dispatchPerformanceCommand({ ...nudge, edit: { ...nudge.edit } }));
		// Control: a reset queued after them closes the merge window, so the
		// press after it must not jump ahead into the nudge before the reset.
		presses.push(entry.dispatchPerformanceCommand({ type: 'grid_edit', deck: 2, edit: { kind: 'reset', part: 'adjust' } }));
		presses.push(entry.dispatchPerformanceCommand({ ...nudge, edit: { ...nudge.edit } }));
		release();
		// The re-read fails here (no /anlz fixture); only the writes are under test.
		await Promise.allSettled(presses);
		assert.deepEqual(writes, [['nudge', 2], ['nudge', 10], ['reset', 'adjust'], ['nudge', 2]],
			'five repeats behind a write are one request; nothing crosses the reset');
	} finally {
		Object.assign(deck, prior);
		uninstall();
		globalThis.fetch = originalFetch;
		globalThis.window = originalWindow;
	}
});

test('[if] a preset takes the lock while a nudge is queued [then] a later press is refused, not merged ahead of it, [else stop]', async () => {
	const entry = await loadTypeScriptModule('tests/unit/fixtures/grid-edit-ipc-entry.ts', {
		viteApiBase: 'https://grid-edit-preset-lock.example.test'
	});
	const SID_P = 'e'.repeat(40);
	const originalFetch = globalThis.fetch;
	const originalWindow = globalThis.window;
	const writes = [];
	let release;
	const held = new Promise((resolve) => { release = resolve; });
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		const path = new URL(request.url).pathname;
		if (request.method === 'POST' && path.endsWith('/grid-edit/nudge')) {
			const body = JSON.parse(await request.text());
			writes.push(['nudge', body.offset_ms]);
			if (writes.length === 1) await held;
		} else {
			return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
		}
		return new Response(JSON.stringify({ stable_id: SID_P, source: 'rekordbox', offset_ms: 0, beat_shift: 0, modified_at: null }), {
			status: 200, headers: { 'content-type': 'application/json' }
		});
	};
	globalThis.window = {};
	const uninstall = entry.installPerformanceBrowserIpc();
	const deck = entry.deckStates[2];
	const prior = { stable_id: deck.stable_id, load_generation: deck.load_generation, anlz: deck.anlz };
	const presetId = 'grid-nudge-lock-test';
	let prepared = null;
	try {
		Object.assign(deck, { stable_id: SID_P, load_generation: 4, anlz: { beatgrid: { source: 'rekordbox', beats: [{ t: 0.5, n: 1, bpm: 120 }] } } });
		const nudge = () => ({ type: 'grid_edit', deck: 2, edit: { kind: 'nudge', offset_ms: 2, beats: 0 } });
		const presses = [entry.dispatchPerformanceCommand(nudge())];
		await new Promise((resolve) => setTimeout(resolve, 10));
		// Control: before the lock, a repeat still joins the queued nudge.
		presses.push(entry.dispatchPerformanceCommand(nudge()));
		prepared = entry.preparePerformancePresetTransaction(presetId, async () => {});
		await assert.rejects(entry.dispatchPerformanceCommand(nudge()), /owns controls at .*command grid_edit rejected/);
		release();
		await Promise.allSettled([...presses, prepared]);
		assert.deepEqual(writes, [['nudge', 2], ['nudge', 2]], 'the refused press wrote nothing and joined nothing');
	} finally {
		release();
		if (prepared !== null) {
			await prepared.catch(() => {});
			entry.abortPreparedPerformancePreset(presetId, 'test release');
		}
		Object.assign(deck, prior);
		uninstall();
		globalThis.fetch = originalFetch;
		globalThis.window = originalWindow;
	}
});

// Sol P1 on PR #6235: GRIDEDIT-6235 says a held button repeats after 350 ms,
// so the first repeat fires at the delay, not one interval later.
test('hold-to-repeat fires at the delay, then every interval, until stopped', () => {
	let now = 0;
	const timers = [];
	const fake = {
		setTimeout: (fn, ms) => (timers.push({ fn, at: now + ms, every: null }), timers.length - 1),
		setInterval: (fn, ms) => (timers.push({ fn, at: now + ms, every: ms }), timers.length - 1),
		clearTimeout: (id) => (timers[id] = null),
		clearInterval: (id) => (timers[id] = null)
	};
	const advanceTo = (t) => {
		for (;;) {
			const due = timers.map((x, i) => [x, i]).filter(([x]) => x !== null && x.at <= t).sort((a, b) => a[0].at - b[0].at)[0];
			if (due === undefined) break;
			const [timer, id] = due;
			now = timer.at;
			if (timer.every === null) timers[id] = null;
			else timer.at += timer.every;
			timer.fn();
		}
		now = t;
	};
	const fired = [];
	const stop = edit.startHoldRepeat(() => fired.push(now), 350, 80, fake);
	advanceTo(349);
	assert.deepEqual(fired, [], 'nothing before the delay');
	advanceTo(600);
	assert.deepEqual(fired, [350, 430, 510, 590]);
	stop();
	advanceTo(1000);
	assert.equal(fired.length, 4, 'nothing after release');
	const early = [];
	const stopEarly = edit.startHoldRepeat(() => early.push(now), 350, 80, fake);
	stopEarly();
	advanceTo(2000);
	assert.deepEqual(early, [], 'a release before the delay repeats nothing');
});

test('the adjust button repeats through startHeldGridNudge, pinned to the pressed target, with the documented timings', () => {
	const buttons = readFileSync(new URL('../../src/lib/components/rb/deck/GridEditButtons.svelte', import.meta.url), 'utf8');
	assert.match(
		buttons,
		/startHeldGridNudge\(\s*\(\) => submit\(intent\),\s*gridHoldTarget\(deck\),\s*\(\) => gridHoldTarget\(deck\),\s*GRID_ADJUST_REPEAT_DELAY_MS,\s*GRID_ADJUST_REPEAT_INTERVAL_MS\s*\)/
	);
	assert.doesNotMatch(buttons, /setInterval\(/);
});

// Codex P2 on PR #6235: an agent or MIDI load while grid adjust is held must
// not keep nudging the newly loaded track.
test('a held grid adjust stops repeating when the deck loads another track or switches grid', () => {
	const run = (change, changeAt = 440) => {
		let now = 0;
		const timers = [];
		const fake = {
			setTimeout: (fn, ms) => (timers.push({ fn, at: now + ms, every: null }), timers.length - 1),
			setInterval: (fn, ms) => (timers.push({ fn, at: now + ms, every: ms }), timers.length - 1),
			clearTimeout: (id) => (timers[id] = null),
			clearInterval: (id) => (timers[id] = null)
		};
		const advanceTo = (t) => {
			for (;;) {
				const due = timers.map((x, i) => [x, i]).filter(([x]) => x !== null && x.at <= t).sort((a, b) => a[0].at - b[0].at)[0];
				if (due === undefined) break;
				const [timer, id] = due;
				now = timer.at;
				if (timer.every === null) timers[id] = null;
				else timer.at += timer.every;
				timer.fn();
			}
			now = t;
		};
		let deck = { stable_id: 'rb:1', load_generation: 3, anlz: { beatgrid: { source: 'rekordbox' } } };
		const fired = [];
		edit.startHeldGridNudge(() => fired.push(now), edit.gridHoldTarget(deck), () => edit.gridHoldTarget(deck), 350, 80, fake);
		advanceTo(changeAt);
		if (changeAt === 440) assert.deepEqual(fired, [350, 430], 'repeats while the target holds');
		if (change !== null) deck = { ...deck, ...change };
		advanceTo(1000);
		return { fired, live: timers.filter((x) => x !== null).length };
	};
	const same = run(null);
	assert.deepEqual(same.fired, [350, 430, 510, 590, 670, 750, 830, 910, 990], 'control: an unchanged deck keeps repeating');
	for (const change of [
		{ stable_id: 'rb:2', load_generation: 4 },
		{ load_generation: 4 },
		{ anlz: { beatgrid: { source: 'own' } } }
	]) {
		const changed = run(change);
		assert.deepEqual(changed.fired, [350, 430], `no repeat after ${JSON.stringify(change)}`);
		assert.equal(changed.live, 0, 'and the repeat timers are cleared');
	}
	// Codex P3 and Sentry on PR #8127: a change inside the first delay stops
	// the hold before any repeat, and leaves no interval behind.
	const early = run({ load_generation: 4 }, 100);
	assert.deepEqual(early.fired, [], 'no nudge after a change inside the first delay');
	assert.equal(early.live, 0, 'and no interval is left running');
	// Control: unchanged before the first delay, it still repeats from 350.
	assert.deepEqual(run(null, 100).fired, [350, 430, 510, 590, 670, 750, 830, 910, 990]);
});

test('Rust engine mode refuses grid edits by name and greys both buttons (Codex P2 BLOCKING, PR #6235)', () => {
	const buttons = readFileSync(new URL('../../src/lib/components/rb/deck/GridEditButtons.svelte', import.meta.url), 'utf8');
	assert.equal(buttons.match(/data-rust-command="grid_edit"/g)?.length, 2, 'both grid buttons go inert in Rust mode');
	const rustMode = readFileSync(new URL('../../src/lib/audio-engine/rust-mode.svelte.ts', import.meta.url), 'utf8');
	const webAudioOnly = rustMode.match(/export const WEB_AUDIO_ONLY: ReadonlySet<string> = new Set\(\[([^\]]+)\]/);
	assert.ok(webAudioOnly, 'rust-mode.svelte.ts declares WEB_AUDIO_ONLY');
	assert.match(webAudioOnly[1], /'grid_edit'/, 'the grid_edit command is refused by name, not sent to the page engine');
});


// ---------- undo (GRIDEDIT-10215) ---------------------------------------------

test('Cmd/Ctrl+Z on a focused grid button is undo; plain z, Cmd+Shift+Z and Cmd+Arrow are not', () => {
	assert.deepEqual(edit.intentForKey('adjust', 'z', false, true), { kind: 'undo' });
	assert.deepEqual(edit.intentForKey('shift', 'Z', false, true), { kind: 'undo' });
	assert.equal(edit.intentForKey('adjust', 'z', false, false), null);
	assert.equal(edit.intentForKey('adjust', 'z', false), null);
	assert.equal(edit.intentForKey('adjust', 'z', true, true), null, 'shift is kept for a redo');
	assert.equal(edit.intentForKey('adjust', 'ArrowLeft', false, true), null, 'Cmd+Arrow is the bar jump');
	assert.deepEqual(edit.intentForKey('adjust', 'ArrowLeft', false, false), { kind: 'nudge', offset_ms: -2, beats: 0 });
});

test('the primary modifier is Cmd on a Mac and Ctrl elsewhere', () => {
	assert.equal(edit.primaryModifierHeld({ metaKey: true, ctrlKey: false }, 'MacIntel'), true);
	assert.equal(edit.primaryModifierHeld({ metaKey: false, ctrlKey: true }, 'MacIntel'), false);
	assert.equal(edit.primaryModifierHeld({ metaKey: false, ctrlKey: true }, 'Win32'), true);
	assert.equal(edit.primaryModifierHeld({ metaKey: true, ctrlKey: false }, 'Linux x86_64'), false);
});

test('grid_edit accepts an undo with only its kind', () => {
	assert.deepEqual(command.parseGridEditIntent({ kind: 'undo' }), { kind: 'undo' });
	assert.throws(() => command.parseGridEditIntent({ kind: 'undo', part: 'adjust' }), TypeError);
});

test('an undo after a nudge runs after it, is never merged, and the drain re-reads once', async () => {
	const fake = fakePorts();
	const q = queueMod.createGridEditQueue(fake.ports);
	q.submit(SID, 'rekordbox', { kind: 'nudge', offset_ms: 2, beats: 0 });
	q.submit(SID, 'rekordbox', { kind: 'undo' });
	await settle(fake, q);
	assert.deepEqual(fake.calls.map((call) => call[0]), ['nudge', 'undo', 'refresh']);
});

test('both available grid titles mention the undo chord', () => {
	const anlz = anlzWith([{ t: 0.5, n: 1, bpm: 120 }]);
	assert.match(edit.gridAdjustTitle(anlz, null), /Cmd\+Z/);
	assert.match(edit.gridShiftTitle(anlz, null), /Cmd\+Z/);
});
