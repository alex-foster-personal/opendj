// requirement: SAMPLER-14629
// requirement: SAMPLER-14677
/**
 * Issue #9987: the classic sampler unit core (4 banks x 16 slots), pure.
 *
 * Regression lines:
 * - if bank 4 or slot 16 is addressed then the address is refused
 * - if a load names both sources then it is refused, and neither clears the slot
 * - if a GET view is read and written back then the PUT document holds the same slots
 * - if a loaded slot fires then the plan is one shot, launch off, own tempo, level 100, whole source
 * - if an empty, missing or undecoded slot fires then the reason names the bank and slot
 * - if a sounding loop is pressed then it stops; a sounding one shot or gate restarts
 * - if a gate is released then it stops; one shot and loop ignore the release
 * - if slot and master gains are set then the voice level is 100 x 10^(sum/20), capped at 100
 * - if a gain is out of range, a mode unknown or the slot empty then the edit is refused
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let L;
before(async () => {
	L = await loadTypeScriptModule('src/lib/player/sampler/classic-sampler-logic.ts');
});

const emptyView = () => ({
	v: 1,
	active_bank: 0,
	banks: Array.from({ length: 4 }, () => Array(16).fill(null))
});

test('the unit is 4 x 16 and addresses outside it are refused', () => {
	/** [if] a fresh unit is built and bank 4 or slot 16 is addressed [then] 64 empty slots and refusals, [else stop]. */
	const unit = L.emptyUnit();
	assert.equal(unit.banks.length, 4);
	assert.ok(unit.banks.every((b) => b.length === 16 && b.every((s) => s === null)));
	assert.throws(() => L.slotAddress(4, 0), /bank must be 0\.\.3, got 4/);
	assert.throws(() => L.slotAddress(0, 16), /slot must be 0\.\.15, got 16/);
	assert.throws(() => L.slotAddress(0, 1.5), /slot must be 0\.\.15/);
	assert.equal(L.slotLabel(L.slotAddress(1, 2)), 'B3');
	assert.equal(L.slotLabel(L.slotAddress(3, 15)), 'D16');
});

test('a load fills one slot from exactly one source, and no source clears it', () => {
	/** [if] a track and a file are loaded, both at once, then neither [then] filled, refused, cleared, [else stop]. */
	const a = L.slotAddress(0, 3);
	let unit = L.loadSlot(L.emptyUnit(), a, {
		stable_id: 't1',
		title: 'Track One',
		duration_ms: 4000
	});
	assert.deepEqual(L.slotAt(unit, a), {
		source: { kind: 'track', stable_id: 't1' },
		title: 'Track One',
		mode: 'one_shot',
		gain_db: 0,
		duration_ms: 4000,
		missing: false
	});
	const f = L.slotAddress(2, 0);
	unit = L.loadSlot(unit, f, { path: '/music/horn.wav' });
	assert.equal(L.slotAt(unit, f).title, 'horn.wav');
	assert.equal(L.slotAt(unit, f).duration_ms, null);
	assert.throws(
		() => L.loadSlot(unit, a, { stable_id: 't', path: '/x.wav' }),
		/bank A slot 4: .*not both/
	);
	assert.throws(
		() => L.loadSlot(unit, a, { path: '/x.wav', duration_ms: 0 }),
		/bank A slot 4: duration_ms/
	);
	unit = L.loadSlot(unit, a, {});
	assert.equal(L.slotAt(unit, a), null);
	assert.ok(L.slotAt(unit, f) !== null);
});

test('the banks document round-trips the server shape', () => {
	/** [if] a GET view with a track, a missing track and a file is read then written [then] same slots back, [else stop]. */
	const view = emptyView();
	view.active_bank = 2;
	view.banks[0][0] = { stable_id: 't1', mode: 'loop', gain_db: -3, title: 'A', missing: false };
	view.banks[1][5] = { stable_id: 'gone', mode: 'one_shot', gain_db: 0, title: 'B', missing: true };
	view.banks[3][15] = { path: '/c.wav', mode: 'gate', gain_db: 6, title: 'C', missing: false };
	const unit = L.unitFromBanksView(view, new Map([['track:t1', 1500]]));
	assert.equal(unit.active_bank, 2);
	assert.equal(L.slotAt(unit, L.slotAddress(0, 0)).duration_ms, 1500);
	assert.equal(L.slotAt(unit, L.slotAddress(1, 5)).missing, true);
	const doc = L.banksDocOf(unit);
	assert.deepEqual(doc.banks[0][0], { stable_id: 't1', mode: 'loop', gain_db: -3, title: 'A' });
	assert.deepEqual(doc.banks[1][5], {
		stable_id: 'gone',
		mode: 'one_shot',
		gain_db: 0,
		title: 'B'
	});
	assert.deepEqual(doc.banks[3][15], { path: '/c.wav', mode: 'gate', gain_db: 6, title: 'C' });
	assert.equal(doc.banks.flat().filter((s) => s !== null).length, 3);
	assert.equal(doc.v, 1);
	assert.equal(doc.active_bank, 2);
	view.banks[0][1] = { path: '/d.wav', mode: 'reverse', gain_db: 0, title: 'D', missing: false };
	assert.throws(() => L.unitFromBanksView(view), /bank A slot 2: unknown mode reverse/);
	view.banks[0][1] = { path: '/d.wav', mode: 'loop', gain_db: 7, title: 'D', missing: false };
	assert.throws(() => L.unitFromBanksView(view), /bank A slot 2: gain 7 dB/);
	assert.throws(() => L.unitFromBanksView({ ...emptyView(), banks: [[]] }), /4 banks of 16 slots/);
});

test('a loaded slot fires the whole source once at unity on the shared engine', () => {
	/** [if] a 4 s track slot fires [then] one shot, launch off, own tempo, key off, level 100, 0..4000 ms, [else stop]. */
	const a = L.slotAddress(1, 2);
	const unit = L.loadSlot(L.emptyUnit(), a, { stable_id: 't9', title: 'Nine', duration_ms: 4000 });
	const plan = L.slotTriggerPlan(unit, a);
	assert.equal(plan.ok, true);
	const s = plan.engineSlot;
	assert.equal(s.slot_id, `classic:B3:${encodeURIComponent('track:t9')}`);
	assert.deepEqual(
		[s.in_ms, s.out_ms, s.stable_id, s.title, s.vector],
		[0, 4000, 't9', 'Nine', null]
	);
	assert.deepEqual(s.settings, {
		launch: 'off',
		key: 'off',
		tempo: 'own',
		mode: 'one_shot',
		repeat: 'once',
		downbeat_offset: 0,
		level: 100
	});
	const reloaded = L.loadSlot(unit, a, { path: '/other.wav', duration_ms: 900 });
	assert.notEqual(L.slotTriggerPlan(reloaded, a).engineSlot.slot_id, s.slot_id);
});

test('an empty, missing or undecoded slot is refused with a reason naming it', () => {
	/** [if] an empty, a missing and a length-unknown slot fire [then] each reason names bank and slot, [else stop]. */
	const view = emptyView();
	view.banks[0][1] = { stable_id: 'gone', mode: 'one_shot', gain_db: 0, title: 'G', missing: true };
	view.banks[0][2] = { path: '/x.wav', mode: 'one_shot', gain_db: 0, title: 'X', missing: false };
	const unit = L.unitFromBanksView(view);
	assert.deepEqual(L.slotTriggerPlan(unit, L.slotAddress(3, 0)), {
		ok: false,
		reason: 'bank D slot 1 is empty'
	});
	assert.match(
		L.slotTriggerPlan(unit, L.slotAddress(0, 1)).reason,
		/^bank A slot 2: .*gone from the library/
	);
	assert.match(
		L.slotTriggerPlan(unit, L.slotAddress(0, 2)).reason,
		/^bank A slot 3: .*length is not known/
	);
});

test('press and release transitions per play mode', () => {
	/** [if] each mode is pressed silent, pressed sounding and released [then] start, stop/restart, gate-only stop, [else stop]. */
	for (const mode of L.BANK_MODES) assert.equal(L.pressAction(mode, false), 'start');
	assert.equal(L.pressAction('loop', true), 'stop');
	assert.equal(L.pressAction('one_shot', true), 'restart');
	assert.equal(L.pressAction('gate', true), 'restart');
	assert.deepEqual(
		L.BANK_MODES.map((m) => L.releaseAction(m)),
		['none', 'none', 'stop']
	);
});

test('a slot plays in its mode: loop repeats the whole source, gate and one shot play once', () => {
	/** [if] a 2 s slot is set to loop, then gate [then] loop/loop then gate/once, region 0..2000 ms, 1 beat, [else stop]. */
	const a = L.slotAddress(0, 0);
	let unit = L.loadSlot(L.emptyUnit(), a, { path: '/l.wav', duration_ms: 2000 });
	unit = L.setSlotMode(unit, a, 'loop');
	const loop = L.slotTriggerPlan(unit, a).engineSlot;
	assert.deepEqual(
		[loop.settings.mode, loop.settings.repeat, loop.in_ms, loop.out_ms, loop.beats],
		['loop', 'loop', 0, 2000, 1]
	);
	unit = L.setSlotMode(unit, a, 'gate');
	const gate = L.slotTriggerPlan(unit, a).engineSlot.settings;
	assert.deepEqual([gate.mode, gate.repeat, gate.launch], ['gate', 'once', 'off']);
	assert.throws(() => L.setSlotMode(unit, a, 'reverse'), /bank A slot 1: unknown mode reverse/);
	assert.throws(() => L.setSlotMode(unit, L.slotAddress(0, 1), 'loop'), /bank A slot 2 is empty/);
});

test('slot gain and master gain combine into the voice level, capped at unity', () => {
	/** [if] slot -6 and master -6 dB, then slot +6 at master 0 [then] level 100x10^(-12/20), then 100, [else stop]. */
	assert.equal(L.dbToLinear(0), 1);
	assert.ok(Math.abs(L.dbToLinear(-6) - 0.501187) < 1e-6);
	const a = L.slotAddress(2, 3);
	let unit = L.loadSlot(L.emptyUnit(), a, { stable_id: 't', duration_ms: 1000 });
	unit = L.setSlotGain(unit, a, -6);
	const level = L.slotTriggerPlan(unit, a, -6).engineSlot.settings.level;
	assert.ok(Math.abs(level - 100 * 10 ** (-12 / 20)) < 1e-9);
	unit = L.setSlotGain(unit, a, 6);
	assert.equal(L.slotTriggerPlan(unit, a, 0).engineSlot.settings.level, 100);
	assert.equal(L.banksDocOf(unit).banks[2][3].gain_db, 6);
});

test('gains outside their ranges are refused', () => {
	/** [if] slot gain 7 or -25 dB, or master gain 1 or -61 dB [then] each is refused naming the value, [else stop]. */
	const a = L.slotAddress(0, 0);
	const unit = L.loadSlot(L.emptyUnit(), a, { path: '/g.wav', duration_ms: 1000 });
	assert.throws(() => L.setSlotGain(unit, a, 7), /bank A slot 1: gain 7 dB/);
	assert.throws(() => L.setSlotGain(unit, a, -25), /gain -25 dB/);
	assert.throws(() => L.checkedMasterGain(1), /master gain 1 dB/);
	assert.throws(() => L.slotTriggerPlan(unit, a, -61), /master gain -61 dB/);
	assert.equal(L.checkedMasterGain(-60), -60);
});

test('the shared engine plans a classic loop as the whole buffer and a one shot as one pass', async () => {
	/** [if] a 2 s classic loop and one shot are planned by the engine's voice plan [then] loop 0..2 s, then no loop, [else stop]. */
	const P = await loadTypeScriptModule('src/lib/player/sampler/sampler-voice-plan.ts');
	const a = L.slotAddress(0, 0);
	let unit = L.loadSlot(L.emptyUnit(), a, { path: '/p.wav', duration_ms: 2000 });
	const once = P.buildVoicePlan(L.slotTriggerPlan(unit, a).engineSlot, {}, null);
	assert.deepEqual([once.loop, once.regionSec], [false, 2]);
	unit = L.setSlotMode(unit, a, 'loop');
	const loop = P.buildVoicePlan(L.slotTriggerPlan(unit, a).engineSlot, {}, null);
	assert.deepEqual([loop.loop, loop.loopStartSec, loop.loopEndSec], [true, 0, 2]);
});
