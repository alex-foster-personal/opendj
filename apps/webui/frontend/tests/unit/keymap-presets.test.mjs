// requirement: KEYB-11488
// [if] a preset binding omits a published default or shadows the library arrows [then] this fails, [else stop].
// [if] PUT ui-prefs keyboard shape drifts from what these presets expose [then] this fails, [else stop].
/**
 * KEYB-11488: rekordbox and Serato keyboard layout presets.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const keymap = await loadTypeScriptModule('src/lib/keyboard/keymap.ts');
const commands = await loadTypeScriptModule('src/lib/keyboard/keymap-commands.ts');
const presets = await loadTypeScriptModule('src/lib/keyboard/presets/index.ts');
const record = await loadTypeScriptModule('src/lib/keyboard/keymap-record.ts');

const KEY_PART_TO_CODE = {
	'-': 'Minus',
	'=': 'Equal',
	'[': 'BracketLeft',
	']': 'BracketRight',
	';': 'Semicolon',
	"'": 'Quote',
	Space: 'Space',
	Enter: 'Enter',
	Escape: 'Escape',
	Tab: 'Tab',
	Backspace: 'Backspace',
	ArrowUp: 'ArrowUp',
	ArrowDown: 'ArrowDown',
	ArrowLeft: 'ArrowLeft',
	ArrowRight: 'ArrowRight'
};

function keyPartToCode(key) {
	if (/^[A-Z]$/.test(key)) return `Key${key}`;
	if (/^\d$/.test(key)) return `Digit${key}`;
	if (/^F\d{1,2}$/.test(key)) return key;
	const named = KEY_PART_TO_CODE[key];
	if (named !== undefined) return named;
	throw new Error(`synthEvent: unknown key part ${JSON.stringify(key)}`);
}

function synthEvent(chord, platform) {
	const parts = chord.split('+');
	const mods = new Set(parts.slice(0, -1));
	const key = parts[parts.length - 1];
	const usesMeta = /mac|iphone|ipad/i.test(platform);
	return {
		code: keyPartToCode(key),
		metaKey: usesMeta ? mods.has('Cmd') || mods.has('Meta') : mods.has('Meta'),
		ctrlKey: usesMeta ? mods.has('Ctrl') : mods.has('Cmd') || mods.has('Ctrl'),
		altKey: mods.has('Alt'),
		shiftKey: mods.has('Shift')
	};
}

function everyPresetChord(presetModule) {
	const chords = new Set();
	for (const id of presetModule.KEYMAP_PRESET_IDS) {
		const map = presetModule.KEYMAP_PRESETS[id];
		for (const list of Object.values(map.bindings)) {
			for (const chord of list) chords.add(chord);
		}
	}
	return [...chords];
}

test('rekordbox deck chords resolve on global/deck scopes', () => {
	const rb = presets.KEYMAP_PRESETS.rekordbox;
	const scopes = ['global', 'deck'];
	const resolve = (chord) =>
		keymap.resolveKeymapKeydown(rb, synthEvent(chord, 'MacIntel'), { activeScopes: scopes });
	assert.equal(resolve('Z'), 'deck.1.play');
	assert.equal(resolve('N'), 'deck.2.play');
	assert.equal(resolve('A'), 'deck.1.cue');
	assert.equal(resolve('H'), 'deck.2.cue');
	assert.equal(resolve('Shift+ArrowLeft'), 'deck.1.load');
	assert.equal(resolve('1'), 'deck.1.hot_cue.A');
});

test('serato deck chords resolve', () => {
	const scopes = ['global', 'deck'];
	const serato = presets.KEYMAP_PRESETS.serato;
	const resolve = (chord) =>
		keymap.resolveKeymapKeydown(serato, synthEvent(chord, 'MacIntel'), { activeScopes: scopes });
	assert.equal(resolve('W'), 'deck.1.play');
	assert.equal(resolve('S'), 'deck.2.play');
	assert.equal(resolve('Shift+ArrowLeft'), 'deck.1.load');
	for (let i = 0; i < 5; i += 1) {
		assert.equal(resolve(String(i + 1)), `deck.1.hot_cue.${'ABCDE'[i]}`);
	}
});

test('keymap targets for rekordbox samples', () => {
	assert.deepEqual(commands.keymapCommandTarget('deck.1.play'), {
		kind: 'midi',
		action: { type: 'deck_play_toggle', deck: 1 }
	});
	assert.deepEqual(commands.keymapCommandTarget('deck.1.cue'), {
		kind: 'midi',
		action: { type: 'deck_cue', deck: 1 }
	});
	assert.deepEqual(commands.keymapCommandTarget('deck.1.load'), {
		kind: 'midi',
		action: { type: 'browse_load', deck: 1 }
	});
	assert.deepEqual(commands.keymapCommandTarget('deck.1.hot_cue.A'), {
		kind: 'hot_cue',
		deck: 1,
		slot: 'A',
		mode: 'set_or_trigger'
	});
});

test('presets have no internal chord conflicts', () => {
	for (const id of presets.KEYMAP_PRESET_IDS) {
		assert.deepEqual(keymap.keymapConflicts(presets.KEYMAP_PRESETS[id]), []);
	}
	const bad = {
		id: 'bad',
		label: 'bad',
		bindings: { 'transport.toggle': ['Z'], 'comment.pin': ['Z'] }
	};
	assert.ok(keymap.keymapConflicts(bad).length > 0);
});

test('chordFromEvent round-trips every preset chord', () => {
	const chords = everyPresetChord(presets);
	for (const platform of ['MacIntel', 'Win32']) {
		for (const chord of chords) {
			const event = synthEvent(chord, platform);
			assert.equal(keymap.chordFromEvent(event, platform), chord, `${chord} on ${platform}`);
		}
	}
	assert.equal(
		keymap.chordFromEvent({
			code: 'ShiftLeft',
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: true
		}),
		null
	);
});

test('unboundCommands lists rekordbox mixer and layout gaps', () => {
	const unbound = presets.unboundCommands('rekordbox').map((c) => c.id);
	assert.ok(unbound.includes('mixer.fader_up.1'));
	assert.ok(unbound.includes('comment.pin'));
	assert.ok(unbound.includes('layout.deck_less'));
	assert.ok(unbound.includes('transport.toggle'));
	assert.ok(unbound.includes('deck.3.play'));
	assert.ok(!unbound.includes('deck.1.play'));
	assert.ok(!unbound.includes('deck.2.load'));
	for (const id of Object.keys(presets.KEYMAP_PRESETS.rekordbox.bindings)) {
		assert.ok(commands.keymapCommandById(id), `unknown binding id ${id}`);
	}
});

test('effectiveKeymap overlays overrides and warns once', () => {
	const serato = presets.KEYMAP_PRESETS.serato;
	const warn = [];
	const original = console.warn;
	console.warn = (...args) => warn.push(args.join(' '));
	const merged = keymap.effectiveKeymap(serato, { 'deck.1.play': ['Enter'], nope: ['X'] });
	console.warn = original;
	assert.equal(
		keymap.resolveKeymapKeydown(merged, synthEvent('Enter', 'MacIntel'), {
			activeScopes: ['deck', 'global']
		}),
		'deck.1.play'
	);
	assert.equal(
		keymap.resolveKeymapKeydown(merged, synthEvent('W', 'MacIntel'), {
			activeScopes: ['deck', 'global']
		}),
		null
	);
	assert.equal(warn.length, 1);
	assert.match(warn[0], /nope/);
	const rbMerged = keymap.effectiveKeymap(presets.KEYMAP_PRESETS.rekordbox, {
		'deck.1.play': ['Enter']
	});
	assert.equal(
		keymap.resolveKeymapKeydown(rbMerged, synthEvent('Enter', 'MacIntel'), {
			activeScopes: ['deck', 'global']
		}),
		'deck.1.play'
	);
});

test('library movement yield respects serato W/S only', () => {
	assert.equal(keymap.libraryMovementYields(presets.KEYMAP_PRESETS.serato, 'w'), true);
	assert.equal(keymap.libraryMovementYields(presets.KEYMAP_PRESETS.serato, 's'), true);
	assert.equal(keymap.libraryMovementYields(presets.KEYMAP_PRESETS.rekordbox, 'w'), false);
	assert.equal(keymap.libraryMovementYields(presets.KEYMAP_PRESETS.opendj, 'w'), false);
	for (const arrow of ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']) {
		for (const id of presets.KEYMAP_PRESET_IDS) {
			const map = presets.KEYMAP_PRESETS[id];
			assert.ok(
				!Object.values(map.bindings).some((chords) => chords.includes(arrow)),
				`${id} must not bind bare ${arrow}`
			);
		}
	}
});

test('legacy gate and opendj Space ownership', () => {
	assert.equal(keymap.legacyPerformanceChordsActive('opendj'), true);
	assert.equal(keymap.legacyPerformanceChordsActive('rekordbox'), false);
	const opendj = presets.KEYMAP_PRESETS.opendj;
	assert.equal(
		keymap.resolveKeymapKeydown(opendj, synthEvent('Space', 'MacIntel'), {
			activeScopes: ['global', 'deck']
		}),
		'transport.toggle'
	);
	assert.equal(keymap.keymapOwnsKeydown({ preset: 'opendj', overrides: {} }, 'Space'), false);
});

test('keyboard_preset catalog row is searchable', () => {
	const catalogPath = fileURLToPath(new URL('../../src/lib/settings/catalog.ts', import.meta.url));
	const text = readFileSync(catalogPath, 'utf8');
	assert.match(text, /id: 'keyboard_preset'/);
	assert.match(text, /group: 'keyboard'/);
	assert.match(text, /value: 'rekordbox'/);
	assert.match(text, /value: 'serato'/);
});

test('record helper saves free chord and refuses conflict', () => {
	const opendj = presets.KEYMAP_PRESETS.opendj;
	const saved = record.recordKeymapFromKeydown(
		opendj,
		{},
		'deck.3.play',
		{ ...synthEvent('Enter', 'MacIntel'), key: 'Enter' },
		'MacIntel'
	);
	assert.equal(saved.kind, 'saved');
	const conflict = record.recordKeymapFromKeydown(
		opendj,
		{},
		'comment.pin',
		{ ...synthEvent('Space', 'MacIntel'), key: ' ' },
		'MacIntel'
	);
	assert.equal(conflict.kind, 'conflict');
	const cancelled = record.recordKeymapFromKeydown(
		opendj,
		{},
		'deck.3.play',
		{ ...synthEvent('Escape', 'MacIntel'), key: 'Escape' },
		'MacIntel'
	);
	assert.equal(cancelled.kind, 'cancelled');
});
