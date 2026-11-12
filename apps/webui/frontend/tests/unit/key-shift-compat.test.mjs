import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * KEYSHIFT-01..05 (the maintainer, Tue 13 Oct 2026): the compatible filter's Transition
 * and Mashup modes, the allowed key shift, the superscript and the key hover.
 *
 * [if] a semitone-out track never matches [then] Mashup can't rescue the
 *   near-miss pairs the feature exists for - broken.
 * [if] a shifted match beats an unshifted one [then] tracks that need no
 *   stretch show a superscript and load shifted for nothing - broken.
 * [if] Mashup accepts an adjacent wheel key [then] it is just Transition
 *   with a different name - broken.
 * [if] the shift-0 Transition rule drifts from camelotKeysWithinSteps
 *   [then] turning the shift off silently changes the old filter - broken.
 */

let camelot;
let filter;
let copy;
let prefs;

before(async () => {
	camelot = await loadTypeScriptModule('src/lib/player/key/camelot.ts');
	filter = await loadTypeScriptModule('src/lib/rb/next-only-filter.ts');
	copy = await loadTypeScriptModule('src/lib/rb/compatible-filter-copy.ts');
	prefs = await loadTypeScriptModule('src/lib/rb/compatible-filter-prefs.ts');
});

const KEYS = [];
for (const mode of ['A', 'B']) for (let n = 1; n <= 12; n += 1) KEYS.push(`${n}${mode}`);

const DEFAULT_RULES = ['same_key', 'adjacent', 'relative'];
const TRANSITION = { mode: 'transition', rules: DEFAULT_RULES };
const MASHUP = { mode: 'mashup', relative: false };

function matchCount(rule, maxShift, ref = '8A') {
	return KEYS.filter((k) => camelot.compatibleKeyShift(k, ref, rule, maxShift) !== null).length;
}

describe('compatibleKeyShift', () => {
	it('rescues a semitone-out mashup: Bb minor (3A) loads down 1 into A minor (8A)', () => {
		assert.deepEqual(camelot.compatibleKeyShift('3A', '8A', MASHUP, 1), { shift: -1, key: '8A' });
		assert.deepEqual(camelot.compatibleKeyShift('1A', '8A', MASHUP, 1), { shift: 1, key: '8A' });
	});

	it('an unshifted match always wins and carries shift 0', () => {
		assert.deepEqual(camelot.compatibleKeyShift('8A', '8A', MASHUP, 3), { shift: 0, key: '8A' });
		assert.deepEqual(camelot.compatibleKeyShift('9A', '8A', TRANSITION, 3), { shift: 0, key: '9A' });
	});

	it('Mashup is the exact key only: adjacent and relative keys do not match unshifted', () => {
		assert.equal(camelot.compatibleKeyShift('9A', '8A', MASHUP, 0), null);
		assert.equal(camelot.compatibleKeyShift('8B', '8A', MASHUP, 0), null);
		assert.deepEqual(
			camelot.compatibleKeyShift('8B', '8A', { ...MASHUP, relative: true }, 0),
			{ shift: 0, key: '8B' }
		);
	});

	it('the smallest shift wins, and up beats down at equal size', () => {
		// 2A is 2 semitones from 8A's family either way only through +1/-1 family members;
		// every candidate must be the minimal magnitude the order allows.
		for (const k of KEYS) {
			const m = camelot.compatibleKeyShift(k, '8A', TRANSITION, 3);
			if (m === null || m.shift === 0) continue;
			for (const smaller of camelot.compatibleShiftOrder(Math.abs(m.shift) - 1)) {
				const shifted = camelot.effectiveCamelotKey(k, smaller);
				assert.equal(
					camelot.camelotRulesAllow('8A', shifted, DEFAULT_RULES),
					false,
					`${k}: a smaller shift ${smaller} already matched, but ${m.shift} was chosen`
				);
			}
			if (m.shift < 0) {
				const up = camelot.effectiveCamelotKey(k, -m.shift);
				assert.equal(camelot.camelotRulesAllow('8A', up, DEFAULT_RULES), false, `${k}: up ${-m.shift} also matched; up must win the tie`);
			}
		}
		assert.deepEqual(camelot.compatibleShiftOrder(2), [0, 1, -1, 2, -2]);
	});

	it('with no shift, Transition equals the Camelot rule checkboxes (camelotRulesAllow) for every pair', () => {
		for (const rules of [DEFAULT_RULES, ['same_key'], ['diagonal', 'energy_boost'], camelot.CAMELOT_RULE_IDS]) {
			for (const a of KEYS) {
				for (const b of KEYS) {
					assert.equal(
						camelot.compatibleKeyShift(a, b, { mode: 'transition', rules }, 0) !== null,
						camelot.camelotRulesAllow(b, a, rules),
						`${a} vs ${b} under ${rules.join('+')}`
					);
				}
			}
		}
	});

	it('matches the counts the research note and Settings copy state (of 24 keys)', () => {
		assert.deepEqual([0, 1, 2, 3].map((n) => matchCount(MASHUP, n)), [1, 3, 5, 7]);
		assert.deepEqual([0, 1, 2, 3].map((n) => matchCount(TRANSITION, n)), [4, 11, 17, 19]);
	});

	it('refuses an out-of-range shift max and returns null for unparseable keys', () => {
		assert.throws(() => camelot.compatibleKeyShift('8A', '8A', MASHUP, 4), RangeError);
		assert.throws(() => camelot.compatibleKeyShift('8A', '8A', MASHUP, 1.5), RangeError);
		assert.equal(camelot.compatibleKeyShift('nope', '8A', MASHUP, 1), null);
		assert.equal(camelot.compatibleKeyShift('8A', null, MASHUP, 1), null);
	});

	it('musical notation works the same as Camelot (Bbm against Am)', () => {
		assert.deepEqual(camelot.compatibleKeyShift('Bbm', 'Am', MASHUP, 1), { shift: -1, key: '8A' });
	});
});

describe('filter wiring', () => {
	const base = () => ({ ...prefs.COMPATIBLE_FILTER_DEFAULTS, bpm_enabled: false });

	it('defaults: Transition on the default Camelot rules, shift 1, Mashup exact', () => {
		assert.equal(prefs.COMPATIBLE_FILTER_DEFAULTS.mode, 'transition');
		assert.equal(prefs.COMPATIBLE_FILTER_DEFAULTS.key_shift_max, 1);
		assert.deepEqual(prefs.COMPATIBLE_FILTER_DEFAULTS.rules, DEFAULT_RULES);
		assert.equal(prefs.COMPATIBLE_FILTER_DEFAULTS.mashup_relative, false);
	});

	it('isAppropriateNext follows the mode: 9A passes Transition and fails Mashup', () => {
		const ref = { key: '8A', bpm: 124 };
		assert.equal(filter.isAppropriateNext({ key: '9A', bpm: 124 }, ref, base()), true);
		assert.equal(filter.isAppropriateNext({ key: '9A', bpm: 124 }, ref, { ...base(), mode: 'mashup' }), false);
		assert.equal(filter.isAppropriateNext({ key: '3A', bpm: 124 }, ref, { ...base(), mode: 'mashup' }), true);
		assert.equal(
			filter.isAppropriateNext({ key: '3A', bpm: 124 }, ref, { ...base(), mode: 'mashup', key_shift_max: 0 }),
			false
		);
	});

	it('the BPM window still applies to a shifted key match', () => {
		const ref = { key: '8A', bpm: 124 };
		const p = { ...base(), mode: 'mashup', bpm_enabled: true, bpm_window_bpm: 10, allow_half_double: false };
		assert.equal(filter.isAppropriateNext({ key: '3A', bpm: 160 }, ref, p), false);
	});

	it('validateCompatibleFilterPrefs accepts the new fields and refuses bad ones', () => {
		assert.deepEqual(
			prefs.validateCompatibleFilterPrefs({ mode: 'mashup', key_shift_max: 3 }, 'k'),
			{ mode: 'mashup', key_shift_max: 3 }
		);
		assert.throws(() => prefs.validateCompatibleFilterPrefs({ mode: 'blend' }, 'k'), /compatible_filter.mode invalid/);
		assert.throws(() => prefs.validateCompatibleFilterPrefs({ key_shift_max: 4 }, 'k'), /key_shift_max invalid/);
	});
});

describe('copy', () => {
	it('key hover: lettered left, Camelot right, minor with a lowercase m', () => {
		assert.deepEqual(copy.letteredAndCamelot('2A'), { lettered: 'Ebm', quality: 'Eb minor', camelot: '2A' });
		assert.deepEqual(copy.letteredAndCamelot('8B'), { lettered: 'C', quality: 'C major', camelot: '8B' });
		assert.equal(copy.letteredAndCamelot('??'), null);
	});

	it('superscript: +1, U+2212 minus for down, empty for none', () => {
		assert.equal(copy.keyShiftSuperscript(1), '+1');
		assert.equal(copy.keyShiftSuperscript(-2), '−2');
		assert.equal(copy.keyShiftSuperscript(0), '');
	});

	it('shift line names the own key and the direction', () => {
		assert.equal(copy.keyShiftHoverLine('3A', -1), 'Its own key: Bbm · 3A. Loads down 1 semitone.');
		assert.equal(copy.keyShiftHoverLine('3A', 0), null);
	});

	it('a shifted key renders in the chosen notation and fails loudly when it cannot (review 4395715256)', () => {
		assert.equal(copy.shiftedKeyText('8A', 'camelot'), '8A');
		assert.notEqual(copy.shiftedKeyText('8A', 'musical'), '8A', 'musical notation must not fall back to Camelot');
		assert.throws(() => copy.shiftedKeyText('not-a-key', 'musical'), /does not render/);
		assert.throws(() => copy.shiftedKeyText('', 'camelot'), /does not render/);
	});

	it('compatibleRowKeyShift has no default prefs (review 4395715251)', async () => {
		const src = await readFile(new URL('../../src/lib/rb/next-only-filter.ts', import.meta.url), 'utf8');
		const sig = src.slice(src.indexOf('export function compatibleRowKeyShift('), src.indexOf('): CompatibleKeyShift | null {'));
		assert.match(sig, /prefs: CompatibleFilterPrefs\n/);
		assert.doesNotMatch(sig, /= COMPATIBLE_FILTER_DEFAULTS/);
	});

	it('mode definition follows the settings', () => {
		const p = { ...prefs.COMPATIBLE_FILTER_DEFAULTS };
		assert.equal(
			copy.compatibleModeDefinition(p),
			'Same key, ±1 same letter, Relative (A/B), after a shift of up to 1 semitone.'
		);
		assert.equal(
			copy.compatibleModeDefinition({ ...p, rules: [] }),
			'Key not checked (no Camelot rule ticked): BPM alone decides.'
		);
		assert.equal(
			copy.compatibleModeDefinition({ ...p, mode: 'mashup', key_shift_max: 0 }),
			'The exact same key, with no key shift.'
		);
	});
});

describe('load command parsing', () => {
	let ipc;
	before(async () => {
		ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	});

	// Found live: the source-shape test passed while the real load was
	// refused with "unexpected fields: key_shift". Parse the actual command.
	it('a load carrying key_shift parses, and a zero shift is dropped', () => {
		assert.deepEqual(
			ipc.parsePerformanceCommandForTest({ type: 'load', deck: 2, stable_id: 'a', key_shift: -1 }),
			{ type: 'load', deck: 2, stable_id: 'a', key_shift: -1 }
		);
		assert.deepEqual(
			ipc.parsePerformanceCommandForTest({ type: 'load', deck: 2, stable_id: 'a', key_shift: 0 }),
			{ type: 'load', deck: 2, stable_id: 'a' }
		);
	});

	// A shifted load runs KEY nudges that feed KEY SYNC followers, so it must
	// serialize with sync-scoped commands like key_nudge does (Codex P1).
	it('a shifted load claims the sync scope; an unshifted load stays deck-only', () => {
		assert.deepEqual(
			ipc.performanceCommandQueueScopes({ type: 'load', deck: 2, stable_id: 'a', key_shift: -2 }),
			[2, 'sync']
		);
		assert.deepEqual(
			ipc.performanceCommandQueueScopes({ type: 'load', deck: 2, stable_id: 'a' }),
			[2]
		);
		assert.deepEqual(
			ipc.performanceCommandQueueScopes({ type: 'key_nudge', deck: 2, semitones: 1 }),
			[2, 'sync']
		);
	});

	it('refuses a key_shift outside ±3 or not an integer', () => {
		for (const bad of [4, -4, 1.5, '1']) {
			assert.throws(
				() => ipc.parsePerformanceCommandForTest({ type: 'load', deck: 2, stable_id: 'a', key_shift: bad }),
				RangeError,
				String(bad)
			);
		}
	});
});

describe('UI wiring (source)', () => {
	const read = (p) => readFile(new URL(`../../src/lib/${p}`, import.meta.url), 'utf8');

	it('the dropdown opens on a 70 ms hover debounce', async () => {
		const src = await read('components/rb/browser/CompatibleFilterPopover.svelte');
		assert.match(src, /OPEN_DEBOUNCE_MS = 70;/);
		assert.match(src, /onmouseenter=\{_openSoon\}/);
	});

	it("a shifted row renders its superscript and the hover card replaces the native title", async () => {
		const table = await read('components/rb/browser/TrackTable.svelte');
		assert.match(table, /<sup\s+class="key-shift-sup">\{keyShiftSuperscript\(match\.shift\)\}<\/sup\s*>/);
		assert.match(table, /<KeyHoverCard/);
		const cell = table.slice(table.indexOf('class="c-key"'), table.indexOf('class="c-key"') + 600);
		assert.doesNotMatch(cell.slice(0, cell.indexOf('>')), /\btitle=/);
		assert.match(cell, /data-custom-tip=""/);
		assert.match(table, /getBoundingClientRect\(\)/);
		assert.match(table, /anchor:\s*\{\s*left:\s*rect\.left,\s*top:\s*rect\.top,\s*width:\s*rect\.width,\s*height:\s*rect\.height\s*\}/);
		assert.match(table, /anchor=\{keyHover\.anchor\}/);
		assert.equal((table.match(/<KeyHoverCard/g) ?? []).length, 1);

		const card = await read('components/rb/browser/KeyHoverCard.svelte');
		assert.match(card, /placeFloating\(/);
		assert.match(card, /preferred:\s*'above'/);
		assert.match(card, /gap:\s*6/);
		assert.doesNotMatch(card, /translate\(-50%/);
	});

	it('KeyHoverCard throws when ResizeObserver is unavailable', async () => {
		const card = await read('components/rb/browser/KeyHoverCard.svelte');
		assert.match(card, /if \(node === null\) return;/);
		assert.doesNotMatch(
			card,
			/if \(node === null \|\| typeof ResizeObserver === 'undefined'\) return;/
		);
		assert.match(card, /if \(typeof ResizeObserver === 'undefined'\) \{/);
		assert.match(card, /throw new Error\('KeyHoverCard requires ResizeObserver'\);/);
	});

	it('a library load carries the row key shift, and the dispatcher applies it after the load', async () => {
		const panel = await read('components/rb/BrowserPanel.svelte');
		assert.match(panel, /\.\.\.\(keyShift !== 0 \? \{ key_shift: keyShift \} : \{\}\)/);
		const ipc = await read('rb/performance-ipc.svelte.ts');
		assert.match(ipc, /deckLoadSettled\(\);\s*\}\s*if \(command\.key_shift !== undefined && command\.key_shift !== 0\) \{\s*await _applyLoadKeyShift/);
		const rust = await read('audio-engine/rust-mode.svelte.ts');
		assert.match(rust, /command\.type === 'load' && command\.key_shift !== undefined/);
	});
});

describe('BrowserPanel shifted load', () => {
	// Replacing the deck that supplies the compatible reference unloads it
	// first, which clears the reference, so the row's shift must be read
	// before that unload (Codex P1 on #6574).
	it('reads the row key shift before unloading the target deck', async () => {
		const src = await readFile(
			new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url),
			'utf8'
		);
		const snapshot = src.indexOf('const keyShift = opts.noKeyShift === true ? 0 : _compatibleLoadKeyShift(row);');
		const unload = src.indexOf("type: 'unload', deck: target, refuseIfMaster: true");
		const load = src.indexOf("...(keyShift !== 0 ? { key_shift: keyShift } : {})");
		assert.ok(snapshot > 0 && unload > 0 && load > 0, 'all three markers present');
		assert.equal(src.indexOf('_compatibleLoadKeyShift(row)', snapshot + 70), -1);
		assert.ok(snapshot < unload && unload < load);
	});
});

describe('compatible reference callers', () => {
	// Cmd-K built its reference slice without the shift, so it filtered
	// against the source key (Codex P1 on #6574). Every caller must pass it.
	it('every computeNextOnlyRef caller passes the deck key shift', async () => {
		const read = (p) => readFile(new URL(`../../src/lib/components/rb/${p}`, import.meta.url), 'utf8');
		for (const [file, slice] of [
			['CommandBarV2.svelte', /key_shift_semitones: decks\[d\]\.key_shift_semitones/],
			['BrowserPanel.svelte', /key_shift_semitones: s\.key_shift_semitones/]
		]) {
			const src = await read(file);
			const call = src.indexOf('computeNextOnlyRef(');
			assert.ok(call > 0, `${file} calls computeNextOnlyRef`);
			assert.match(src, slice, `${file} passes key_shift_semitones`);
			// COMPATBPM-01/03: the heard tempo and the loaded track's BPM confidence.
			assert.match(src, /pitch: (decks\[d\]|s)\.pitch/, `${file} passes the deck tempo ratio`);
			assert.match(src, /bpm_confidence: bpmConfidenceIn\(/, `${file} passes the deck BPM confidence`);
		}
	});
});

describe('Cmd-K shows and loads no key shift', () => {
	// Cmd-K renders the raw key with no superscript, so a load from it must
	// not shift the deck, and its filter must not offer shift-only matches
	// (Codex P1 on #6574).
	it('both Cmd-K load ports load unshifted; the table path still derives the shift', async () => {
		const src = await readFile(
			new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url),
			'utf8'
		);
		for (const fn of ['commandBarLoad', 'commandBarV2Load']) {
			const body = src.slice(src.indexOf(`export async function ${fn}(`), src.indexOf(`export async function ${fn}(`) + 200);
			// The options literal may carry more than `noKeyShift`: commandBarV2Load
			// also forwards `stillNewest`, and command-bar-v2.test.mjs pins that
			// exact shape. What this test owns is the shift decision, so it asserts
			// `noKeyShift: true` OPENS the literal rather than that the literal
			// closes immediately after it.
			assert.match(body, /_loadOntoDeck\(row, deck, \{ noKeyShift: true\s*[,}]/, fn);
		}
		assert.match(src, /function loadRow\([\s\S]{0,200}void _loadOntoDeck\(row, deck, opts\);/);
	});

	it('the Cmd-K compatible filter keeps exact matches and drops shift-only ones', async () => {
		const filters = await loadTypeScriptModule('src/lib/rb/command-bar-filters.ts');
		const barPrefs = {
			next_only_filter: true,
			hide_broken_links: false,
			remixes_filter: false,
			vocals_filter: false,
			available_offline_filter: false,
			compatible_filter: { ...prefs.COMPATIBLE_FILTER_DEFAULTS, mode: 'mashup', key_shift_max: 1 }
		};
		const ref = { key: '8A', bpm: 124 };
		const keep = filters.cmdkKeep(barPrefs, ref, false);
		const row = (key) => ({ stable_id: key, key, bpm: 124, file_exists: true, is_streaming: false });
		assert.equal(keep(row('8A')), true, 'exact key kept');
		assert.equal(keep(row('3A')), false, '3A matches 8A only after a -1 shift');
		// Control: the library predicate with the same prefs does keep 3A.
		assert.equal(filter.isAppropriateNext(row('3A'), ref, barPrefs.compatible_filter), true);
	});
});

describe('Sol P1 fixes on #6574', () => {
	const read = (p) => readFile(new URL(`../../src/lib/components/rb/${p}`, import.meta.url), 'utf8');

	it('the dropdown keys reach the panel from the checkbox, handled once', async () => {
		// Keyboard focus opens the dropdown but stays on the checkbox; T / M and
		// A/D/W/S were wired only to the panel and so did nothing (Sol P1).
		const pop = await read('browser/CompatibleFilterPopover.svelte');
		assert.match(pop, /bind:this=\{panelRef\}/);
		assert.match(pop, /onkeydown=\{_onKeydown\}/);
		assert.match(pop, /panelRef\.handleKey\(e\);/);
		// Behaviour: a key pressed before the lazy panel binds is held, then
		// handled exactly once by the panel when it binds (review 4395514044).
		const { PendingKeyBuffer } = await loadTypeScriptModule('src/lib/components/rb/browser/pending-key-buffer.ts');
		const buffer = new PendingKeyBuffer();
		const handled = [];
		const panelRef = { handleKey: (e) => handled.push(e.key) };
		const press = (k, bound) => {
			const e = { key: k, metaKey: false, ctrlKey: false, altKey: false, preventDefault() {} };
			if (bound) panelRef.handleKey(e);
			else buffer.hold(e);
		};
		press('m', false);
		assert.deepEqual(handled, [], 'nothing reaches the panel before it binds');
		buffer.flush((e) => panelRef.handleKey(e));
		press('t', true);
		assert.deepEqual(handled, ['m', 't'], 'the held M lands once on bind, then T goes straight through');
		const panel = await read('browser/CompatibleFilterPanel.svelte');
		assert.match(panel, /export function handleKey\(e: KeyboardEvent\): void/);
		assert.doesNotMatch(panel, /onkeydown=/);
	});

	it('a failed key hover-card load is shown as a toast and retried on the next hover', async () => {
		// Sol P1: the rejection was only logged, so the card silently never appeared.
		// The toast lives in BrowserPanel; TrackTable reports through a prop so
		// it adds no import (the fan-out ratchet is at its ceiling).
		const table = await read('browser/TrackTable.svelte');
		assert.match(table, /onkeycardfailed\(`Key card failed to load: \$\{detail\}\. Hover the key again to retry\.`\);/);
		assert.doesNotMatch(table, /console\.warn\('key hover card failed to load'/);
		assert.match(table, /keyHoverCardLoad = null;/);
		const panel = await read('BrowserPanel.svelte');
		assert.match(panel, /onkeycardfailed=\{\(message\) => pushToast\(message, 'error'\)\}/);
		// [if] the reporter prop is optional [then] a caller can drop the failure silently, [else stop]
		assert.match(table, /onkeycardfailed: \(message: string\) => void;/);
	});
});
