/**
 * #12270: the hotkeys overlay's display-only /performance rows against the router that runs them.
 *
 * HOTKEY_REGISTRY transcribes the performance chords by hand (see the header of
 * hotkeys-registry.ts), so nothing but this test keeps a row honest. Each row's displayed chord is
 * parsed into physical US-layout key presses, and every press is fed through
 * handlePerformanceShortcutKeydown, the dispatcher performance-hotkeys.ts calls, with recording
 * handlers. The reverse sweep presses every key under every Cmd / Opt / Shift combination.
 *
 * Scope: rows whose id starts with `performance-`, default context (the `deck.snapped_jump` flag
 * is off by default, so Cmd+arrows are unbound; no dial or fader selected). Out of scope: the
 * selector controls (SELECTOR_CONTROL_HOTKEYS, bound by a later PR), the Electron-only edge
 * sampler branch inside installPerformanceHotkeys, and Ctrl, which the router treats as Cmd.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let routing;
let registry;
const hadElement = 'Element' in globalThis;

before(async () => {
	// The Space gate asks `target instanceof Element`; a plain object target is page chrome.
	if (!hadElement) globalThis.Element = class Element {};
	routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
	registry = await loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-registry.ts');
});

after(() => {
	if (!hadElement) delete globalThis.Element;
});

// ----- US layout: [code, label, unshifted key, shifted key] -----

const LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
	.split('')
	.map((c) => [`Key${c}`, c, c.toLowerCase(), c]);
const DIGITS = '1234567890'.split('').map((d, i) => [`Digit${d}`, d, d, '!@#$%^&*()'[i]]);
const PUNCTUATION = [
	['Backquote', '`', '`', '~'],
	['Minus', '-', '-', '_'],
	['Equal', '=', '=', '+'],
	['BracketLeft', '[', '[', '{'],
	['BracketRight', ']', ']', '}'],
	['Backslash', '\\', '\\', '|'],
	['Semicolon', ';', ';', ':'],
	['Quote', "'", "'", '"'],
	['Comma', ',', ',', '<'],
	['Period', '.', '.', '>'],
	['Slash', '/', '/', '?'],
	['Space', 'Space', ' ', ' ']
];
const NAMED = [
	'Tab',
	'Enter',
	'Escape',
	'Backspace',
	'ArrowLeft',
	'ArrowRight',
	'ArrowUp',
	'ArrowDown',
	'PageUp',
	'PageDown',
	'Home',
	'End'
].map((k) => [k, k, k, k]);
const US_KEYS = [...LETTERS, ...DIGITS, ...PUNCTUATION, ...NAMED];

const MODIFIERS = ['Cmd', 'Opt', 'Shift'];

/** One physical press as `Cmd+Opt+Shift+<label>` (modifiers in that order). */
function pressLabel({ label, cmd, opt, shift }) {
	return [cmd && 'Cmd', opt && 'Opt', shift && 'Shift', label].filter(Boolean).join('+');
}

function keyEvent({ code, unshifted, shifted, cmd, opt, shift }) {
	return {
		code,
		key: shift ? shifted : unshifted,
		target: { tagName: 'DIV', getAttribute: () => null },
		metaKey: cmd,
		ctrlKey: false,
		altKey: opt,
		shiftKey: shift,
		repeat: false,
		timeStamp: 0,
		preventDefault() {},
		stopPropagation() {}
	};
}

/** The handler calls one press makes, or null when the router leaves the key alone. */
function routedCalls(press) {
	const calls = [];
	const record =
		(name) =>
		(...args) =>
			calls.push([name, ...args]);
	const handlers = {
		toggleRecentPlay: (_pressT0Ms, quantize) => calls.push(['toggleRecentPlay', quantize]),
		resizeLast: record('resizeLast'),
		exitLast: record('exitLast'),
		armPinPlacement: record('armPinPlacement'),
		waveZoomStep: record('waveZoomStep'),
		gridJump: record('gridJump'),
		sectionJump: record('sectionJump'),
		recentDeckCommand: record('recentDeckCommand'),
		cycleRecentDeck: record('cycleRecentDeck'),
		tempoKey: record('tempoKey'),
		hotCueTrigger: record('hotCueTrigger'),
		hotCueSave: record('hotCueSave'),
		hotCueClear: record('hotCueClear'),
		selectorCommand: record('selectorCommand'),
		clearSelector: record('clearSelector'),
		selectorLeafCommand: record('selectorLeafCommand')
	};
	const handled = routing.handlePerformanceShortcutKeydown(keyEvent(press), handlers, {});
	return handled ? calls : null;
}

/** Map one displayed key token (`+`, `K`, `Space`) to its physical key and implied Shift. */
function physicalKey(token) {
	for (const [code, label, unshifted, shifted] of US_KEYS) {
		if (token === label || token === unshifted) return { code, label, unshifted, shifted };
		if (token === shifted) return { code, label, unshifted, shifted, impliedShift: true };
	}
	throw new Error(`overlay token has no US-layout key: ${JSON.stringify(token)}`);
}

/** Every physical press a displayed chord promises: `Shift+, .`, `` ` / Shift+` ``, `Cmd+Shift+1-8`. */
function chordPresses(chord) {
	return chord.split(' / ').flatMap((alternative) => {
		const parts = /^((?:(?:Cmd|Opt|Shift)\+)*)(.+)$/.exec(alternative);
		const mods = new Set(parts[1].split('+').filter(Boolean));
		return parts[2].split(' ').flatMap((token) => {
			const range = /^(\d)-(\d)$/.exec(token);
			const tokens = range
				? Array.from({ length: Number(range[2]) - Number(range[1]) + 1 }, (_, i) =>
						String(Number(range[1]) + i)
					)
				: [token];
			return tokens.map((t) => {
				const key = physicalKey(t);
				return {
					...key,
					cmd: mods.has('Cmd'),
					opt: mods.has('Opt'),
					shift: mods.has('Shift') || key.impliedShift === true
				};
			});
		});
	});
}

function performanceRows() {
	return registry.HOTKEY_REGISTRY.filter((row) => row.id.startsWith('performance-'));
}

/**
 * The overlay rows that can name a scanned (US printable) /performance key: the performance rows
 * plus the selector's letter rows (#12254, W A S D / E / Q). Its Esc, Enter and arrow rows sit
 * outside US_KEYS, so the scan below never presses them.
 */
function overlayRows() {
	const selectorLetters = registry.SELECTOR_CONTROL_HOTKEYS.filter((row) =>
		/^[A-Z]$/.test(row.chord)
	);
	return [...performanceRows(), ...selectorLetters];
}

const hotCues = (handler, prefix) =>
	Object.fromEntries('ABCDEFGH'.split('').map((slot, i) => [`${prefix}${i + 1}`, [handler, slot]]));

// KEYB-13946 (#12270): every display-only /performance overlay row matches the router that runs it.
/** What each row's presses must do: change a binding and this, plus the row, change together. */
const EXPECTED_CALLS = {
	'performance-space': {
		Space: ['toggleRecentPlay', false],
		'Cmd+Space': ['toggleRecentPlay', true]
	},
	'performance-loop-double': { 'Shift+=': ['resizeLast', 2] },
	'performance-loop-halve': { '-': ['resizeLast', 0.5] },
	'performance-loop-exit': { 'Shift+0': ['exitLast'] },
	'performance-wave-zoom-out': { '[': ['waveZoomStep', 'out'] },
	'performance-wave-zoom-in': { ']': ['waveZoomStep', 'in'] },
	'performance-section-jump-next': { 'Shift+]': ['sectionJump', 'next'] },
	'performance-section-jump-previous': { 'Shift+[': ['sectionJump', 'previous'] },
	'performance-tempo-fine': {
		',': ['tempoKey', { kind: 'tempo-step', direction: -1, coarse: false }],
		'.': ['tempoKey', { kind: 'tempo-step', direction: 1, coarse: false }]
	},
	'performance-tempo-coarse': {
		'Shift+,': ['tempoKey', { kind: 'tempo-step', direction: -1, coarse: true }],
		'Shift+.': ['tempoKey', { kind: 'tempo-step', direction: 1, coarse: true }]
	},
	'performance-tempo-reset': { 'Shift+K': ['tempoKey', { kind: 'tempo-reset' }] },
	'performance-pitch-range-cycle': { P: ['tempoKey', { kind: 'pitch-range-cycle' }] },
	'performance-comment-pin': { M: ['armPinPlacement'], 'Cmd+Shift+M': ['armPinPlacement'] },
	'performance-cue': { C: ['recentDeckCommand', 'cue'] },
	'performance-beat-sync': { V: ['recentDeckCommand', 'beat-sync'] },
	'performance-master-tempo': { K: ['recentDeckCommand', 'master-tempo'] },
	'performance-cycle-deck': { '`': ['cycleRecentDeck', 1], 'Shift+`': ['cycleRecentDeck', -1] },
	'performance-hot-cue-trigger': hotCues('hotCueTrigger', ''),
	'performance-hot-cue-save': hotCues('hotCueSave', 'Shift+'),
	'performance-hot-cue-clear': hotCues('hotCueClear', 'Cmd+Shift+')
};

// [if] a shipped performance binding changes [then] its overlay row's presses stop matching the pinned calls, [else stop].
test('every performance overlay row presses exactly the handler calls pinned for it', () => {
	const actual = Object.fromEntries(
		performanceRows().map((row) => [
			row.id,
			Object.fromEntries(
				chordPresses(row.chord).map((press) => [pressLabel(press), routedCalls(press)?.[0] ?? null])
			)
		])
	);
	assert.deepEqual(actual, EXPECTED_CALLS);
});

// [if] a performance registry chord has no live routed binding [then] the failure names the row id, [else stop].
test('every performance overlay row chord reaches a live routed handler', () => {
	const unrouted = performanceRows()
		.filter((row) => chordPresses(row.chord).some((press) => routedCalls(press) === null))
		.map((row) => row.id);
	assert.deepEqual(unrouted, [], `overlay rows with no live binding: ${unrouted.join(', ')}`);
});

// [if] a routed /performance key is pressable but no overlay row names it [then] the failure names the chord, [else stop].
test('every key the performance router takes is named by an overlay row', () => {
	const documented = new Map(
		overlayRows().flatMap((row) =>
			chordPresses(row.chord).map((press) => [pressLabel(press), routedCalls(press)])
		)
	);
	const undocumented = [];
	for (const [code, label, unshifted, shifted] of US_KEYS) {
		for (let bits = 0; bits < 1 << MODIFIERS.length; bits += 1) {
			const press = {
				code,
				label,
				unshifted,
				shifted,
				cmd: (bits & 1) !== 0,
				opt: (bits & 2) !== 0,
				shift: (bits & 4) !== 0
			};
			const calls = routedCalls(press);
			if (calls === null || documented.has(pressLabel(press))) continue;
			// The router ignores Shift on some keys (= for +, Shift+Space): that press is the row's own.
			const twin = documented.get(pressLabel({ ...press, shift: !press.shift }));
			if (twin !== undefined && JSON.stringify(twin) === JSON.stringify(calls)) continue;
			undocumented.push(pressLabel(press));
		}
	}
	assert.deepEqual(undocumented, [], `routed chords missing from the overlay: ${undocumented}`);
});
