// requirement: STEMPERF-11695
/**
 * Stem quick modes ACA / INST / ALL (src/lib/rb/stem-quick-mode.ts).
 *
 * Regression lines:
 * - [if] a quick mode emits a command for a control that is already right
 *   [then] the plan is not minimal, [else stop].
 * - [if] a plan names a control the layout lacks [then] roformer2 decks get
 *   commands the engine cannot drive, [else stop].
 * - [if] applying a plan does not read back as its mode [then] the chip
 *   would not light after a press, [else stop].
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let qm;

before(async () => {
	qm = await loadTypeScriptModule('src/lib/rb/stem-quick-mode.ts');
});

const IDS = ['vocal', 'instrumental', 'drums', 'bass', 'other'];
const DEMUCS = IDS;
const ROFORMER = ['vocal', 'instrumental'];

function stems(available, over = {}) {
	const controls = {};
	for (const id of IDS)
		controls[id] = { muted: false, solo: false, gain: 0.5, ...(over[id] ?? {}) };
	return { available_controls: [...available], controls };
}

/** Engine semantics: an exclusive solo clears every solo first. */
function applyPlan(state, commands) {
	const next = structuredClone(state);
	for (const c of commands) {
		if (c.type === 'stem_solo') {
			if (c.exclusive) for (const id of IDS) next.controls[id].solo = false;
			next.controls[c.stem].solo = c.solo;
		} else if (c.type === 'stem_mute') {
			next.controls[c.stem].muted = c.muted;
		} else {
			throw new Error(`unexpected command ${c.type}`);
		}
	}
	return next;
}

test('[if] neutral demucs4 ACA/INST/ALL are not one exclusive solo / one mute / nothing [then] plans are not minimal, [else stop].', () => {
	const s = stems(DEMUCS);
	assert.deepStrictEqual(qm.quickModePlan(1, 'aca', s), [
		{ type: 'stem_solo', deck: 1, stem: 'vocal', solo: true, exclusive: true }
	]);
	assert.deepStrictEqual(qm.quickModePlan(1, 'inst', s), [
		{ type: 'stem_mute', deck: 1, stem: 'vocal', muted: true }
	]);
	assert.deepStrictEqual(qm.quickModePlan(1, 'all', s), []);
});

test('[if] ALL from vocal solo + drums muted is not unsolo vocal then unmute drums [then] ALL is broken, [else stop].', () => {
	const s = stems(DEMUCS, { vocal: { solo: true }, drums: { muted: true } });
	assert.deepStrictEqual(qm.quickModePlan(2, 'all', s), [
		{ type: 'stem_solo', deck: 2, stem: 'vocal', solo: false },
		{ type: 'stem_mute', deck: 2, stem: 'drums', muted: false }
	]);
});

test('[if] roformer2 plans differ from demucs4 or name a missing control [then] layout handling is broken, [else stop].', () => {
	const neutral = stems(ROFORMER, {
		drums: { muted: true, solo: true },
		bass: { muted: true },
		other: { solo: true }
	});
	assert.deepStrictEqual(qm.quickModePlan(1, 'aca', neutral), [
		{ type: 'stem_solo', deck: 1, stem: 'vocal', solo: true, exclusive: true }
	]);
	assert.deepStrictEqual(qm.quickModePlan(1, 'inst', neutral), [
		{ type: 'stem_mute', deck: 1, stem: 'vocal', muted: true }
	]);
	assert.deepStrictEqual(qm.quickModePlan(1, 'all', neutral), []);
	assert.equal(qm.quickModeActive(neutral), 'all');
	const busy = stems(ROFORMER, {
		vocal: { muted: true },
		instrumental: { solo: true, muted: true },
		drums: { muted: true }
	});
	for (const mode of qm.STEM_QUICK_MODES) {
		for (const c of qm.quickModePlan(1, mode, busy))
			assert.ok(ROFORMER.includes(c.stem), `${mode} named ${c.stem}`);
	}
});

test('[if] quickModeActive misreads aca / inst / all / custom [then] chips light wrongly, [else stop].', () => {
	const cases = [
		[{}, 'all'],
		[{ vocal: { solo: true } }, 'aca'],
		[{ vocal: { solo: true, muted: true } }, 'custom'],
		[{ vocal: { solo: true }, drums: { solo: true } }, 'custom'],
		[{ vocal: { muted: true } }, 'inst'],
		[{ vocal: { muted: true }, bass: { muted: true } }, 'custom'],
		[{ vocal: { muted: true }, drums: { solo: true } }, 'custom'],
		[{ other: { muted: true } }, 'custom']
	];
	for (const [over, want] of cases)
		assert.equal(qm.quickModeActive(stems(DEMUCS, over)), want, JSON.stringify(over));
});

test('[if] ACA from an instrumental deck does not solo then unmute vocal [then] ACA stays silent, [else stop].', () => {
	assert.deepStrictEqual(qm.quickModePlan(3, 'aca', stems(DEMUCS, { vocal: { muted: true } })), [
		{ type: 'stem_solo', deck: 3, stem: 'vocal', solo: true, exclusive: true },
		{ type: 'stem_mute', deck: 3, stem: 'vocal', muted: false }
	]);
});

test('[if] any start state planned to a mode does not read back as it, or re-plans non-empty [then] plans are wrong, [else stop].', () => {
	const STATES = [{}, { muted: true }, { solo: true }];
	for (const layout of [DEMUCS, ROFORMER]) {
		for (let n = 0; n < 3 ** IDS.length; n += 1) {
			const over = {};
			let k = n;
			for (const id of IDS) {
				over[id] = STATES[k % 3];
				k = Math.floor(k / 3);
			}
			const start = stems(layout, over);
			for (const mode of qm.STEM_QUICK_MODES) {
				const plan = qm.quickModePlan(1, mode, start);
				const after = applyPlan(start, plan);
				assert.equal(qm.quickModeActive(after), mode, `${mode} from ${JSON.stringify(over)}`);
				assert.deepStrictEqual(qm.quickModePlan(1, mode, after), []);
				for (const id of IDS) assert.equal(after.controls[id].gain, 0.5);
				if (qm.quickModeActive(start) === mode) assert.deepStrictEqual(plan, []);
			}
		}
	}
});

test('[if] ACA / INST on stems without vocal do not throw [then] failures are silent, [else stop].', () => {
	const s = { available_controls: [], controls: stems(DEMUCS).controls };
	assert.throws(() => qm.quickModePlan(1, 'aca', s), /no vocal stem/);
	assert.throws(() => qm.quickModePlan(1, 'inst', s), /no vocal stem/);
	assert.deepStrictEqual(qm.quickModePlan(1, 'all', s), []);
});
