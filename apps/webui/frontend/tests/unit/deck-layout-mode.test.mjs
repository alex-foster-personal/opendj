import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Pin 862cd3 - MORE/LESS two-deck performance layout (/performance).
//
// LESS must only hide chrome: all four Deck instances stay mounted, decks
// 3/4 keep their transport/DSP state and stay controllable over IPC, and
// only the *visual* wrappers (deck column slot, waveform row, mixer strip)
// collapse. There is no jsdom in this test harness (see load-typescript.mjs
// - plain esbuild + node:test, no window/document unless a test stubs one),
// so component mounting/collapse is verified two ways: (1) pure keyboard
// logic loaded and exercised directly, (2) static source assertions proving
// the markup never gates deck 1-4 behind a conditional and that the CSS
// collapse rules are scoped to strips/rows 3 and 4 only.

const MIXER = fileURLToPath(
	new URL('../../src/lib/components/rb/Mixer.svelte', import.meta.url)
);
const WAVESTACK = fileURLToPath(
	new URL('../../src/lib/components/rb/WaveformStack.svelte', import.meta.url)
);
const PERF_PAGE = fileURLToPath(
	new URL('../../src/routes/performance/+page.svelte', import.meta.url)
);
const HOTKEYS = fileURLToPath(
	new URL('../../src/lib/rb/deck-layout-hotkeys.ts', import.meta.url)
);

class FakeHTMLElement {
	constructor(tagName, { isContentEditable = false } = {}) {
		this.tagName = tagName;
		this.isContentEditable = isContentEditable;
	}
}

test(
	'if LESS stops deck 3/4 audio or removes agent control instead of only hiding chrome then the pin is broken',
	async () => {
		const mixerSource = readFileSync(MIXER, 'utf8');
		const wavestackSource = readFileSync(WAVESTACK, 'utf8');
		const pageSource = readFileSync(PERF_PAGE, 'utf8');

		// All four Deck instances are unconditional markup - no {#if}/{#each
		// over a filtered list} gates deck 1-4 out of the tree. The route
		// still renders exactly the same four <Deck deckId={N} /> tags LESS
		// or MORE.
		for (const id of [1, 2, 3, 4]) {
			assert.match(
				pageSource,
				new RegExp(`<Deck deckId=\\{${id}\\}\\s*/>`),
				`Deck ${id} must be unconditionally mounted, not gated behind an {#if}`
			);
		}
		assert.ok(
			!/\{#if[^}]*deck_layout[^}]*\}\s*<Deck/.test(pageSource),
			'no {#if deck_layout...} may wrap a <Deck> - decks must stay mounted in both modes'
		);

		// Mixer keeps all four channel strips in the DOM (STRIP_ORDER is a
		// static 4-entry array, never filtered by deck_layout) - LESS only
		// changes their CSS collapse state.
		assert.match(mixerSource, /STRIP_ORDER: DeckId\[\] = \[3, 1, 2, 4\]/);
		assert.ok(
			!/STRIP_ORDER\.filter/.test(mixerSource),
			'STRIP_ORDER must never be filtered by deck_layout - all 4 strips stay mounted'
		);

		// WaveformStack keeps all four wave rows in the DOM the same way.
		assert.match(wavestackSource, /deckIds: DeckId\[\] = \[1, 2, 3, 4\]/);
		assert.ok(
			!/deckIds\.filter/.test(wavestackSource),
			'deckIds must never be filtered by deck_layout - all 4 wave rows stay mounted'
		);
	}
);

test('Mixer collapses only strips 3/4 (STRIP_ORDER columns 0 and 3) in LESS, expanding 1/2', () => {
	const source = readFileSync(MIXER, 'utf8');

	// MORE: four equal columns. LESS: STRIP_ORDER is [3,1,2,4], so columns
	// 0 (deck 3) and 3 (deck 4) collapse to 0 and columns 1/2 (decks 1/2)
	// take the released width.
	assert.match(source, /grid-template-columns:\s*repeat\(4,\s*1fr\)/);
	assert.match(source, /\.strips\.less\s*\{[^}]*grid-template-columns:\s*0 1fr 1fr 0;/s);

	// The transition duration is driven by exactly one shared CSS custom
	// property so every deck-layout collapse animates in lockstep.
	assert.match(source, /var\(--rb-deck-layout-duration,\s*200ms\)/);

	// Reads the persisted mode rather than owning private state, and drives
	// the same setter the hotkeys use.
	assert.match(source, /uiPrefs\.deck_layout === 'less'/);
	assert.match(source, /setDeckLayoutMode\('more'\)/);
	assert.match(source, /setDeckLayoutMode\('less'\)/);
});

test('WaveformStack collapses only wave rows 3/4 in LESS, reclaiming the row height', () => {
	const source = readFileSync(WAVESTACK, 'utf8');

	assert.match(
		source,
		/grid-template-rows:\s*repeat\(4,\s*minmax\(var\(--rb-waverow-h\),\s*auto\)\)/
	);
	assert.match(
		source,
		/\.rb-wavestack\.less\s*\{[^}]*grid-template-rows:\s*minmax\(var\(--rb-waverow-h\),\s*auto\) minmax\(var\(--rb-waverow-h\),\s*auto\) 0(?:px)? 0(?:px)?;/s
	);
	assert.match(source, /var\(--rb-deck-layout-duration,\s*200ms\)/);
	assert.match(source, /uiPrefs\.deck_layout === 'less'/);
});

test('reduced motion forces the shared deck-layout duration to zero', () => {
	const source = readFileSync(PERF_PAGE, 'utf8');
	assert.match(source, /--rb-deck-layout-duration/);
	assert.match(
		source,
		/@media \(prefers-reduced-motion: reduce\)\s*\{[^}]*--rb-deck-layout-duration:\s*0ms/s
	);
});

test('performance route installs and uninstalls the deck-layout hotkeys', () => {
	const source = readFileSync(PERF_PAGE, 'utf8');
	assert.match(source, /installDeckLayoutHotkeys/);
	assert.match(source, /uninstallDeckLayoutHotkeys\s*=\s*installDeckLayoutHotkeys\(\)/);
	assert.match(source, /uninstallDeckLayoutHotkeys\(\);/);
});

test('resolveDeckLayoutHotkeyMode maps Cmd/Ctrl+2 -> less and Cmd/Ctrl+4 -> more', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/deck-layout-hotkeys.ts');

	assert.equal(
		mod.resolveDeckLayoutHotkeyMode({ key: '2', metaKey: true, ctrlKey: false, altKey: false, shiftKey: false, target: null }, { settingsOpen: false }),
		'less'
	);
	assert.equal(
		mod.resolveDeckLayoutHotkeyMode({ key: '4', metaKey: false, ctrlKey: true, altKey: false, shiftKey: false, target: null }, { settingsOpen: false }),
		'more'
	);
	// bare digits (no modifier) do nothing
	assert.equal(
		mod.resolveDeckLayoutHotkeyMode({ key: '2', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, target: null }, { settingsOpen: false }),
		null
	);
	// unrelated digit with modifier does nothing
	assert.equal(
		mod.resolveDeckLayoutHotkeyMode({ key: '3', metaKey: true, ctrlKey: false, altKey: false, shiftKey: false, target: null }, { settingsOpen: false }),
		null
	);
});

test('resolveDeckLayoutHotkeyMode is suppressed while the settings overlay is open', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/deck-layout-hotkeys.ts');
	assert.equal(
		mod.resolveDeckLayoutHotkeyMode({ key: '2', metaKey: true, ctrlKey: false, altKey: false, shiftKey: false, target: null }, { settingsOpen: true }),
		null
	);
});

test('resolveDeckLayoutHotkeyMode is suppressed while typing in an input/textarea/contenteditable', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/deck-layout-hotkeys.ts');
	globalThis.HTMLElement = FakeHTMLElement;

	const input = new FakeHTMLElement('INPUT');
	const textarea = new FakeHTMLElement('TEXTAREA');
	const editable = new FakeHTMLElement('DIV', { isContentEditable: true });
	const plainDiv = new FakeHTMLElement('DIV');

	const chord = { key: '4', metaKey: true, ctrlKey: false, altKey: false, shiftKey: false };
	assert.equal(
		mod.resolveDeckLayoutHotkeyMode({ ...chord, target: input }, { settingsOpen: false }),
		null
	);
	assert.equal(
		mod.resolveDeckLayoutHotkeyMode({ ...chord, target: textarea }, { settingsOpen: false }),
		null
	);
	assert.equal(
		mod.resolveDeckLayoutHotkeyMode({ ...chord, target: editable }, { settingsOpen: false }),
		null
	);
	assert.equal(
		mod.resolveDeckLayoutHotkeyMode({ ...chord, target: plainDiv }, { settingsOpen: false }),
		'more'
	);
});

test('installDeckLayoutHotkeys wires resolveDeckLayoutHotkeyMode to the persisted setter, and uninstalls cleanly', async () => {
	const source = readFileSync(HOTKEYS, 'utf8');
	assert.match(source, /resolveDeckLayoutHotkeyMode\(/);
	assert.match(source, /setDeckLayoutMode\(mode\)/);

	const listeners = new Map();
	const storageValues = new Map();
	globalThis.window = {
		addEventListener: (type, fn) => listeners.set(type, fn),
		removeEventListener: (type) => listeners.delete(type),
		localStorage: {
			getItem: (key) => storageValues.get(key) ?? null,
			setItem: (key, value) => storageValues.set(key, value)
		}
	};
	globalThis.HTMLElement = FakeHTMLElement;

	const mod = await loadTypeScriptModule('src/lib/rb/deck-layout-hotkeys.ts');
	const uninstall = mod.installDeckLayoutHotkeys();
	assert.ok(listeners.has('keydown'), 'installDeckLayoutHotkeys must bind a window keydown listener');

	uninstall();
	assert.ok(!listeners.has('keydown'), 'the returned cleanup must remove the keydown listener');

	delete globalThis.window;
});
