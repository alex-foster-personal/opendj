/**
 * Camelot rule checkboxes for the compatible filter (Tue 27 Oct 2026).
 * The cases mirror tests/shared/test_camelot_rules.py: one rule table,
 * generated from apps/shared/harmonic.py, so browser and server agree.
 *
 * [if] a rule matches by number alone [then] 8A -> 9B passes "±1" again - broken.
 * [if] 12 <-> 1 does not wrap [then] 12A loses 1A - broken.
 * [if] next-only-filter ignores prefs.rules [then] ticking a box changes nothing - broken.
 * [if] the panel stops rendering one checkbox per rule [then] a rule cannot be chosen - broken.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let camelot;
let filter;
before(async () => {
	camelot = await loadTypeScriptModule('src/lib/player/key/camelot.ts');
	filter = await loadTypeScriptModule('src/lib/rb/next-only-filter.ts');
});

const CASES = [
	['same_key', '8A', ['8A', 'Am'], ['8B', '9A', '7A']],
	['same_key', '8B', ['8B', 'C'], ['8A']],
	['adjacent', '8A', ['7A', '9A'], ['9B', '7B', '8A', '10A', '8B']],
	['adjacent', '12A', ['11A', '1A'], ['1B', '12B']],
	['adjacent', '1B', ['12B', '2B'], ['12A', '2A']],
	['relative', '8A', ['8B'], ['8A', '9B', '7B']],
	['relative', '8B', ['8A'], ['9A', '7A']],
	['diagonal', '8B', ['9A'], ['7A', '9B', '8A']],
	['diagonal', '8A', ['7B'], ['9B', '7A', '8B']],
	['diagonal', '12B', ['1A'], ['11A']],
	['diagonal', '1A', ['12B'], ['2B']],
	['energy_boost', '8A', ['10A'], ['6A', '10B', '9A']],
	['energy_boost', '11B', ['1B'], ['1A', '9B']],
	['energy_drop', '8A', ['6A'], ['10A', '6B']],
	['energy_drop', '2B', ['12B'], ['4B', '12A']],
	['jaws', '8A', ['3A'], ['3B', '1A', '8A']],
	['jaws', '1B', ['8B'], ['6B', '8A']],
	['pay_attention', '8A', ['5A'], ['11A', '5B']],
	['pay_attention', '2B', ['11B'], ['5B', '11A']]
];

describe('each Camelot rule matches only its move, with its letter', () => {
	for (const [rule, ref, yes, no] of CASES) {
		it(`${rule} from ${ref}`, () => {
			for (const cand of yes) assert.equal(camelot.camelotRulesAllow(ref, cand, [rule]), true, `${ref} -> ${cand}`);
			for (const cand of no) assert.equal(camelot.camelotRulesAllow(ref, cand, [rule]), false, `${ref} -/-> ${cand}`);
		});
	}
	it('every rule has a case', () => {
		assert.deepEqual([...new Set(CASES.map((c) => c[0]))].sort(), [...camelot.CAMELOT_RULE_IDS].sort());
	});
	it('defaults: same key, ±1 same letter, relative on; the rest off', () => {
		assert.deepEqual([...camelot.DEFAULT_CAMELOT_RULE_IDS], ['same_key', 'adjacent', 'relative']);
	});
	it('an unknown rule id throws instead of matching nothing', () => {
		assert.throws(() => camelot.camelotRulesAllow('8A', '8A', ['tritone']), /Unknown Camelot rule/);
		// if an earlier rule matches first then a later unknown id must still throw
		assert.throws(() => camelot.camelotRulesAllow('8A', '8A', ['same_key', 'tritone']), /Unknown Camelot rule/);
		assert.throws(() => camelot.camelotRulesAllow(null, '8A', ['tritone']), /Unknown Camelot rule/);
	});
	it('an unparseable key matches nothing', () => {
		assert.equal(camelot.camelotRulesAllow('8A', null, ['same_key']), false);
		assert.equal(camelot.camelotRulesAllow('All', '8A', ['same_key']), false);
	});
});

describe('the compatible filter reads prefs.rules', () => {
	const ref = { key: '8A', bpm: 124 };
	const prefs = (rules) => ({ ...filter.COMPATIBLE_FILTER_DEFAULTS, rules });
	it('defaults keep same, ±1 same letter and relative, and drop the cross-letter neighbours', () => {
		for (const key of ['8A', '7A', '9A', '8B']) assert.equal(filter.isAppropriateNext({ key, bpm: 124 }, ref), true, key);
		for (const key of ['9B', '7B', '10A']) assert.equal(filter.isAppropriateNext({ key, bpm: 124 }, ref), false, key);
	});
	it('ticking energy boost admits +2 on the same letter only', () => {
		assert.equal(filter.isAppropriateNext({ key: '10A', bpm: 124 }, ref, prefs(['energy_boost'])), true);
		assert.equal(filter.isAppropriateNext({ key: '10B', bpm: 124 }, ref, prefs(['energy_boost'])), false);
		assert.equal(filter.isAppropriateNext({ key: '8A', bpm: 124 }, ref, prefs(['energy_boost'])), false);
	});
	it('no rule ticked: the key is not checked, BPM alone decides', () => {
		assert.equal(filter.isAppropriateNext({ key: '2B', bpm: 124 }, ref, prefs([])), true);
		assert.equal(filter.isAppropriateNext({ key: '2B', bpm: 170 }, ref, prefs([])), false);
	});
});

describe('no rule ticked: a keyless deck can be the reference', () => {
	const decks = [
		{ is_master: true, playing: true, stable_id: 'm', key: null, bpm: 128, key_shift_semitones: 0, pitch: 1, bpm_confidence: null },
		{ is_master: false, playing: true, stable_id: 'o', key: '8A', bpm: 100, key_shift_semitones: 0, pitch: 1, bpm_confidence: null }
	];
	it('BPM-only mode keeps the keyless master as the reference', () => {
		assert.deepEqual(filter.computeNextOnlyRef(decks, false), { key: null, bpm: 128 });
	});
	it('control: with key rules on, a keyless master cannot be the reference', () => {
		assert.deepEqual(filter.computeNextOnlyRef(decks), { key: '8A', bpm: 100 });
	});
	it('both callers pass whether the key is checked (Mashup always, Transition while a rule is ticked)', () => {
		const panel = readFileSync(fileURLToPath(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)), 'utf8');
		assert.match(panel, /computeNextOnlyRef\(slices, compatibleKeyChecked\(uiPrefs\.compatible_filter\)\)/);
		const bar = readFileSync(fileURLToPath(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url)), 'utf8');
		assert.match(bar, /\t\t\tcompatibleKeyChecked\(uiPrefs\.compatible_filter\)\s*\)/);
	});
	it('Mashup checks the key even with no Transition rule ticked', () => {
		assert.equal(filter.compatibleKeyChecked({ ...filter.COMPATIBLE_FILTER_DEFAULTS, mode: 'mashup', rules: [] }), true);
		assert.equal(filter.compatibleKeyChecked({ ...filter.COMPATIBLE_FILTER_DEFAULTS, mode: 'transition', rules: [] }), false);
		const mashup = { ...filter.COMPATIBLE_FILTER_DEFAULTS, mode: 'mashup', rules: [], key_shift_max: 0 };
		assert.equal(filter.isAppropriateNext({ key: '2B', bpm: 124 }, { key: '8A', bpm: 124 }, mashup), false);
		assert.equal(filter.isAppropriateNext({ key: '8A', bpm: 124 }, { key: '8A', bpm: 124 }, mashup), true);
	});
});

it('CompatibleFilterPanel renders one checkbox per generated rule and saves the rule list', () => {
	const src = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/browser/CompatibleFilterPanel.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(src, /\{#each CAMELOT_RULES as rule \(rule\.id\)\}/);
	assert.match(src, /type="checkbox"/);
	assert.match(src, /patchCompatibleFilter\(\{ rules: next \}\)/);
	// if a second tick builds on the committed list then a quick double toggle loses the first change
	assert.match(src, /new Set\(shownRules\)/);
	assert.match(src, /pendingRules \?\? uiPrefs\.compatible_filter\.rules/);
	assert.match(src, /checked=\{shownRules\.includes\(rule\.id\)\}/);
	// if pending is cleared by array identity then the $state proxy never matches and a failed save sticks
	assert.match(src, /if \(request === rulesRequest\) pendingRules = null;/);
	assert.doesNotMatch(src, /pendingRules === next/);
	// if a superseded save's failure is shown then a later successful save leaves a stale error
	assert.match(src, /if \(request !== rulesRequest\) return;/);
	assert.doesNotMatch(src, /camelot_steps/);
});
