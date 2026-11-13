/**
 * LIBUX-04 hotkeys overlay: filter/highlight + "/" hold vs "?" toggle.
 *
 * [if] "/" is held down [then] the overlay shows for as long as it is held
 * and hides on release, while pressing "?" toggles the same overlay on and
 * leaves it up until toggled off
 * [if] the user types while the overlay is open [then] the query filters the
 * list and the matched substring is highlighted yellow in both views
 * [if] Esc is pressed on an empty filter or the X button is clicked [then]
 * the overlay hides, from either view (#12278: Esc first clears a filter)
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { compile } from 'svelte/compiler';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

let filterMod;
let gestureMod;
let registryMod;

before(async () => {
	filterMod = await loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-filter.ts');
	gestureMod = await loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-gesture.ts');
	registryMod = await loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-registry.ts');
});

const SAMPLE = [
	{
		id: 'space',
		chord: 'Space',
		description: 'Toggle play/pause on the most recent deck',
		group: 'Performance'
	},
	{
		id: 'settings',
		chord: 'Cmd+,',
		description: 'Open the settings overlay',
		group: 'Settings'
	},
	{
		id: 'hold',
		chord: '/',
		description: 'Hold to show the hotkeys overlay',
		group: 'Hotkeys overlay'
	}
];

test('if the query is empty [then] every entry is returned unfiltered with no highlight marks', () => {
	const rows = filterMod.filterHotkeys(SAMPLE, '');
	assert.equal(rows.length, SAMPLE.length);
	for (const row of rows) {
		assert.deepEqual(row.chordPieces, [{ text: row.entry.chord, matched: false }]);
		assert.deepEqual(row.descriptionPieces, [{ text: row.entry.description, matched: false }]);
	}
	const fromRegistry = filterMod.filterHotkeys(registryMod.HOTKEY_REGISTRY, '   ');
	assert.equal(fromRegistry.length, registryMod.HOTKEY_REGISTRY.length);
	assert.ok(fromRegistry.length > 0, 'the shipped registry must not be empty');
});

test('if no entry contains the query [then] the result set is empty', () => {
	const rows = filterMod.filterHotkeys(SAMPLE, 'xyzzy-no-such-binding');
	assert.deepEqual(rows, []);
});

test('if a query matches a substring [then] only that run is marked matched', () => {
	const pieces = filterMod.highlightPieces('Toggle play/pause', 'PLAY');
	assert.deepEqual(pieces, [
		{ text: 'Toggle ', matched: false },
		{ text: 'play', matched: true },
		{ text: '/pause', matched: false }
	]);
	const rows = filterMod.filterHotkeys(SAMPLE, 'play');
	assert.equal(rows.length, 1);
	assert.equal(rows[0].entry.id, 'space');
	assert.deepEqual(rows[0].descriptionPieces, [
		{ text: 'Toggle ', matched: false },
		{ text: 'play', matched: true },
		{ text: '/pause on the most recent deck', matched: false }
	]);
	assert.deepEqual(rows[0].chordPieces, [{ text: 'Space', matched: false }]);
});

test('if / is held [then] the overlay shows until release, and ? does not behave the same', () => {
	// [if] "/" is held down [then] the overlay shows for as long as it is held
	// and hides on release, while pressing "?" toggles the same overlay on
	const hold = gestureMod.createHotkeysOverlayMachine();
	assert.equal(hold.isVisible(), false);
	hold.slashKeyDown();
	assert.equal(hold.isVisible(), true, 'hold must show immediately');
	assert.equal(hold.getState().holding, true);
	assert.equal(hold.getState().toggledOn, false);
	hold.slashKeyUp();
	assert.equal(hold.isVisible(), false, 'release must hide when toggle is off');

	const toggle = gestureMod.createHotkeysOverlayMachine();
	toggle.toggle();
	assert.equal(toggle.isVisible(), true);
	toggle.toggle();
	assert.equal(toggle.isVisible(), false);
	assert.notEqual(
		'hold-release',
		'toggle-stays',
		'sanity: the two gestures are named differently on purpose'
	);
});

test('if ? has toggled the overlay on [then] a / hold+release leaves it open', () => {
	const machine = gestureMod.createHotkeysOverlayMachine();
	machine.toggle();
	assert.equal(machine.isVisible(), true);
	machine.slashKeyDown();
	assert.equal(machine.isVisible(), true);
	machine.slashKeyUp();
	assert.equal(machine.isVisible(), true, 'toggle contribution survives hold release');
	assert.equal(machine.getState().holding, false);
	assert.equal(machine.getState().toggledOn, true);
});

test('if hide() (the X button) fires from list view with an active filter [then] the overlay is gone and state is reset', () => {
	// [if] the X button is clicked [then] the overlay hides, from either view and
	// from any filter state
	const machine = gestureMod.createHotkeysOverlayMachine();
	machine.toggle();
	machine.setView('list');
	machine.setQuery('space');
	assert.equal(machine.isVisible(), true);
	assert.equal(machine.getState().view, 'list');
	assert.equal(machine.getState().query, 'space');
	machine.hide();
	assert.equal(machine.isVisible(), false);
	assert.equal(machine.getState().view, 'grid');
	assert.equal(machine.getState().query, '');
	assert.equal(machine.getState().holding, false);
	assert.equal(machine.getState().toggledOn, false);
});

test('if Esc fires while / is held [then] the overlay stays hidden after release', () => {
	const machine = gestureMod.createHotkeysOverlayMachine();
	machine.slashKeyDown();
	assert.equal(machine.isVisible(), true);
	machine.hide();
	assert.equal(machine.isVisible(), false, 'Esc wins over an active hold');
	machine.slashKeyUp();
	assert.equal(machine.isVisible(), false, 'the matching keyup must not reopen it');
});

test('root layout mounts the overlay and installs its hotkeys next to settings', () => {
	const layout = read('src/routes/+layout.svelte');
	assert.match(layout, /installHotkeysOverlayHotkeys\(\)/);
	assert.match(layout, /<HotkeysOverlay \/>/);
	assert.match(layout, /from '\$lib\/components\/rb\/hotkeys\/install-hotkeys-overlay'/);
});

// LIBUX-10456: registry commands with default keys are listed in the overlay.
const PRESET_DEF = {
	id: 'preset.run.bass_swap_8',
	label: 'Bass swap over 8',
	defaultKeys: ['Shift+1'],
	scope: 'global'
};

test('a registry command with a default key yields one entry with its chord, label and id', () => {
	assert.deepEqual(registryMod.hotkeyEntriesFromCommandRegistry([PRESET_DEF]), [
		{
			id: 'preset.run.bass_swap_8',
			chord: 'Shift+1',
			description: 'Bass swap over 8',
			group: 'Transition presets'
		}
	]);
});

test('a registry command with no default key yields no entry', () => {
	assert.deepEqual(
		registryMod.hotkeyEntriesFromCommandRegistry([{ ...PRESET_DEF, defaultKeys: [] }]),
		[]
	);
});

test('several default keys join with " / " and a non-preset command groups by scope', () => {
	const [entry] = registryMod.hotkeyEntriesFromCommandRegistry([
		{ id: 'deck.nudge', label: 'Nudge', defaultKeys: ['[', ']'], scope: 'deck' }
	]);
	assert.equal(entry.chord, '[ / ]');
	assert.equal(entry.group, 'Deck');
});

test('a registry id shared with a hand-written entry keeps one row, the registry one', () => {
	const handWritten = registryMod.HOTKEY_REGISTRY;
	const shared = { ...PRESET_DEF, id: handWritten[0].id };
	const merged = registryMod.mergeHotkeyEntries(
		handWritten,
		registryMod.hotkeyEntriesFromCommandRegistry([shared])
	);
	const ids = merged.map((entry) => entry.id);
	assert.equal(new Set(ids).size, ids.length, `duplicate id in the merged list: ${ids}`);
	assert.equal(
		merged.find((entry) => entry.id === handWritten[0].id).description,
		'Bass swap over 8'
	);
	assert.equal(merged.length, handWritten.length);
});

test('the merged list holds every registry command on main that has default keys, and filterHotkeys still finds the hand-written rows', async () => {
	const commands = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
	const defs = commands.listPerformanceCommands();
	const merged = registryMod.mergeHotkeyEntries(
		registryMod.HOTKEY_REGISTRY,
		registryMod.hotkeyEntriesFromCommandRegistry(defs)
	);
	const ids = new Set(merged.map((entry) => entry.id));
	for (const def of defs.filter((d) => d.defaultKeys.length > 0))
		assert.ok(ids.has(def.id), `${def.id} missing`);
	assert.equal(new Set(merged.map((e) => e.id)).size, merged.length);
	assert.equal(filterMod.filterHotkeys(merged, '   ').length, merged.length);
	for (const entry of registryMod.HOTKEY_REGISTRY)
		assert.ok(ids.has(entry.id), `${entry.id} dropped from the merge`);
});

// if the overlay lacks any of the three Library rows then broken
test('KEYB-11643: library load, instant-double and locate chords are listed under Library', () => {
	const byId = Object.fromEntries(registryMod.HOTKEY_REGISTRY.map((entry) => [entry.id, entry]));
	for (const id of ['library-load-deck', 'library-instant-double', 'library-locate']) {
		assert.equal(byId[id].group, 'Library', id);
	}
	assert.match(byId['library-load-deck'].chord, /Shift\+Left/);
	assert.match(byId['library-instant-double'].chord, /Cmd\+Shift\+Left/);
	assert.equal(byId['library-locate'].chord, 'Cmd+L');
});

// KEYB-13924 (#12254): the keyboard selector's keys are one searchable "Selector controls" group.
async function _overlayEntries(defs) {
	const commands = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
	return registryMod.overlayHotkeyEntries(defs ?? commands.listPerformanceCommands());
}

function _groupNames(rows) {
	return [...new Set(rows.map((row) => row.entry.group))];
}

function _selectorRows(rows) {
	return rows.filter((row) => row.entry.group === registryMod.SELECTOR_CONTROLS_GROUP);
}

/** The selector.* commands as #9395 and #13708 bind them (labels and keys copied from those PRs). */
const BOUND_SELECTOR_DEFS = [
	['selector.move.up', 'Move selector up', ['W']],
	['selector.move.left', 'Move selector left', ['A']],
	['selector.move.down', 'Move selector down', ['S']],
	['selector.move.right', 'Move selector right', ['D']],
	['selector.drill-in', 'Drill into selector', ['E']],
	['selector.drill-out', 'Drill out of selector', ['Q']],
	['selector.activate', 'Activate selected control', ['Enter']],
	['selector.adjust.up', 'Adjust selected control up', ['ArrowUp', 'ArrowRight']],
	['selector.adjust.down', 'Adjust selected control down', ['ArrowDown', 'ArrowLeft']]
].map(([id, label, defaultKeys]) => ({ id, label, defaultKeys, scope: 'global' }));

test('if the overlay opens in grid view [then] one Selector controls group lists W A S D, E, Q, Esc, Enter and the arrows, [else stop].', async () => {
	const rows = filterMod.filterHotkeys(await _overlayEntries(), '');
	assert.equal(_groupNames(rows).filter((g) => g === 'Selector controls').length, 1);
	assert.deepEqual(
		_selectorRows(rows).map((row) => row.entry.chord),
		['W', 'A', 'S', 'D', 'E', 'Q', 'Esc', 'Enter', 'ArrowUp / ArrowRight', 'ArrowDown / ArrowLeft']
	);
});

test('if the overlay is filtered for selector [then] every selector-controls row matches with the group name marked, [else stop].', async () => {
	const entries = await _overlayEntries();
	const rows = filterMod.filterHotkeys(entries, 'selector');
	const expected = entries.filter((entry) => entry.group === 'Selector controls');
	assert.equal(_selectorRows(rows).length, expected.length);
	for (const row of _selectorRows(rows))
		assert.ok(row.groupPieces.some((piece) => piece.matched && /selector/i.test(piece.text)));
});

test('if a selector action is shown [then] its row names move, drill in (enter), drill out (back), clear, activate or adjust, [else stop].', async () => {
	const byChord = Object.fromEntries(
		_selectorRows(filterMod.filterHotkeys(await _overlayEntries(), '')).map((r) => [
			r.entry.chord,
			r.entry.description
		])
	);
	assert.match(byChord.W, /move selector up/i);
	assert.match(byChord.A, /move selector left/i);
	assert.match(byChord.S, /move selector down/i);
	assert.match(byChord.D, /move selector right/i);
	assert.match(byChord.E, /drill into/i);
	assert.match(byChord.Q, /drill out/i);
	assert.match(byChord.Esc, /clear/i);
	assert.match(byChord.Enter, /activate/i);
	assert.match(byChord['ArrowUp / ArrowRight'], /adjust .* up/i);
	assert.match(byChord['ArrowDown / ArrowLeft'], /adjust .* down/i);
});

test('if the selector commands carry default keys [then] their registry rows replace the group one for one, no chord twice, [else stop].', async () => {
	const before = await _overlayEntries([]);
	const after = await _overlayEntries(BOUND_SELECTOR_DEFS);
	assert.deepEqual(after, before);
	const chords = after.map((entry) => entry.chord);
	assert.equal(new Set(chords).size, chords.length, `duplicated chord rows: ${chords}`);
});

//----- #12277: the overlay's toggle control announces the live "?" chord
// KEYB-13970 (#12277): the overlay toggle control carries aria-keyshortcuts from the live "?" constant.

let triggerSsr;

before(async () => {
	triggerSsr = await loadSvelteSsrModule(
		[
			"export { default as HotkeysOverlay } from '$lib/components/rb/hotkeys/HotkeysOverlay.svelte';",
			"export { default as HotkeysOverlayView } from '$lib/components/rb/hotkeys/HotkeysOverlayView.svelte';",
			"export * from '$lib/components/rb/hotkeys/hotkeys-overlay.svelte';",
			"export * from '$lib/components/rb/hotkeys/hotkeys-registry';",
			"export { installHotkeysOverlayHotkeys } from '$lib/components/rb/hotkeys/install-hotkeys-overlay';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

function _closeButton(body) {
	const button = body.match(/<button[^>]*class="hk-close[\s"][^>]*>/);
	assert.ok(button, `no overlay close button in ${body.length} chars of markup`);
	return button[0];
}

function _renderOpenOverlay() {
	triggerSsr.showHotkeysOverlay();
	try {
		return _closeButton(triggerSsr.render(triggerSsr.HotkeysOverlay, { props: {} }).body);
	} finally {
		triggerSsr.hideHotkeysOverlay();
	}
}

function _withFakeWindow(run) {
	const listeners = {};
	const saved = { window: globalThis.window, HTMLElement: globalThis.HTMLElement };
	globalThis.window = {
		addEventListener: (type, fn) => (listeners[type] = fn),
		removeEventListener: () => {}
	};
	globalThis.HTMLElement = class HTMLElement {};
	const uninstall = triggerSsr.installHotkeysOverlayHotkeys();
	try {
		run(listeners);
	} finally {
		uninstall();
		Object.assign(globalThis, saved);
	}
}

function _keydown(key, over = {}) {
	return {
		key,
		code: '',
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: false,
		target: { tagName: 'DIV', getAttribute: () => null },
		preventDefault() {},
		stopPropagation() {},
		...over
	};
}

test('if the overlay trigger is rendered [then] it carries aria-keyshortcuts with Shift+/ for the ? chord, [else stop].', () => {
	assert.equal(triggerSsr.HOTKEYS_OVERLAY_TOGGLE_KEY, '?');
	assert.equal(triggerSsr.HOTKEYS_OVERLAY_TRIGGER.ariaKeyshortcuts, 'Escape Shift+/');
	assert.ok(_renderOpenOverlay().includes('aria-keyshortcuts="Escape Shift+/"'));
});

test('if the toggle key constant changes [then] the trigger aria-keyshortcuts and title follow it, [else stop].', () => {
	const asH = triggerSsr.hotkeysOverlayTrigger('h');
	assert.equal(asH.ariaKeyshortcuts, 'Escape h');
	assert.match(asH.title, /Esc or h\)$/);
	assert.equal(triggerSsr.hotkeysOverlayTrigger('!').ariaKeyshortcuts, 'Escape Shift+1');
	assert.deepEqual(
		triggerSsr.HOTKEYS_OVERLAY_TRIGGER,
		triggerSsr.hotkeysOverlayTrigger(triggerSsr.HOTKEYS_OVERLAY_TOGGLE_KEY)
	);
	const body = triggerSsr.render(triggerSsr.HotkeysOverlayView, {
		props: {
			open: true,
			view: 'grid',
			query: '',
			entries: [],
			trigger: asH,
			onHide() {},
			onSetView() {},
			onSetQuery() {}
		}
	}).body;
	assert.ok(_closeButton(body).includes('aria-keyshortcuts="Escape h"'));
});

test('if the key the trigger announces is pressed [then] the installer toggles the overlay open, then shut, [else stop].', () => {
	const announced = triggerSsr.keyToAriaShortcut(triggerSsr.HOTKEYS_OVERLAY_TOGGLE_KEY);
	assert.equal(announced, 'Shift+/', 'the test presses Shift+/ as a US layout types it');
	_withFakeWindow((listeners) => {
		const press = _keydown(triggerSsr.HOTKEYS_OVERLAY_TOGGLE_KEY, {
			code: 'Slash',
			shiftKey: true
		});
		listeners.keydown(press);
		assert.equal(triggerSsr.isHotkeysOverlayOpen(), true);
		listeners.keydown(press);
		assert.equal(triggerSsr.isHotkeysOverlayOpen(), false);
	});
});

test('if assistive tech reads the trigger [then] its accessible name says it closes the hotkeys overlay, [else stop].', () => {
	const button = _renderOpenOverlay();
	assert.ok(button.includes('aria-label="Close hotkeys overlay"'), button);
	assert.ok(button.includes('title="Hide hotkeys overlay (Esc or ?)"'), button);
});

//----- #12282: one platform formatter names Cmd on Apple and Ctrl elsewhere
// KEYB-13987 (#12282): overlay rows and aria-keyshortcuts name the platform's modifier.

const APPLE = 'MacIntel';
const OTHERS = ['Win32', 'Linux x86_64'];

/** Every overlay chord label, as the open overlay renders it for `platform`. */
function _renderedChords(platform) {
	triggerSsr.showHotkeysOverlay();
	try {
		const body = triggerSsr.render(triggerSsr.HotkeysOverlay, { props: { platform } }).body;
		return [...body.matchAll(/<kbd class="hk-chord[^"]*">([\s\S]*?)<\/kbd>/g)].map((m) =>
			m[1].replace(/<!--[\s\S]*?-->/g, '').trim()
		);
	} finally {
		triggerSsr.hideHotkeysOverlay();
	}
}

function _renderedAria(platform) {
	triggerSsr.showHotkeysOverlay();
	try {
		const body = triggerSsr.render(triggerSsr.HotkeysOverlay, { props: { platform } }).body;
		return [...body.matchAll(/aria-keyshortcuts="([^"]*)"/g)].map((m) => m[1]);
	} finally {
		triggerSsr.hideHotkeysOverlay();
	}
}

test('if the platform is macOS [then] modifier rows display Cmd labels, [else stop].', () => {
	const chords = _renderedChords(APPLE);
	for (const label of ['Cmd+,', 'Cmd+L', 'Cmd+Shift+Z', 'Cmd+Shift+Left / Cmd+Shift+Right'])
		assert.ok(chords.includes(label), `${label} missing from ${chords.length} rendered chords`);
	assert.ok(chords.includes('Ctrl+R'), 'a literal Ctrl chord stays Ctrl on macOS');
	assert.equal(triggerSsr.platformChordLabel('Cmd+Shift+1-8', APPLE), 'Cmd+Shift+1-8');
});

test('if the platform is not macOS [then] equivalent modifier rows display Ctrl labels, [else stop].', () => {
	const apple = _renderedChords(APPLE);
	for (const platform of OTHERS) {
		const chords = _renderedChords(platform);
		assert.equal(chords.length, apple.length, `${platform} lists a different row count`);
		assert.deepEqual(
			chords.filter((c) => /\bCmd\+/.test(c)),
			[],
			`${platform} still shows Cmd`
		);
		for (const label of ['Ctrl+,', 'Ctrl+L', 'Ctrl+Shift+Z', 'Ctrl+Shift+Left / Ctrl+Shift+Right'])
			assert.ok(chords.includes(label), `${platform}: ${label} missing`);
		assert.ok(chords.includes('Right Cmd / Right Ctrl (tap)'), 'a key name is not a modifier');
		const desc = triggerSsr.platformHotkeyEntries(triggerSsr.HOTKEY_REGISTRY, platform);
		assert.ok(!desc.some((e) => /\bCmd\+/.test(e.description)), `${platform} description says Cmd`);
	}
});

test('if a displayed row has aria-keyshortcuts metadata [then] it uses the matching platform modifier, [else stop].', () => {
	assert.equal(triggerSsr.platformChordAria('Cmd+Shift+Z', APPLE), 'Meta+Shift+Z');
	assert.equal(triggerSsr.platformChordAria('Cmd+L / Shift+K', APPLE), 'Meta+L Shift+K');
	assert.equal(triggerSsr.platformChordAria('Ctrl+R', APPLE), 'Control+R');
	assert.equal(triggerSsr.assistantChordAria(APPLE), triggerSsr.platformChordAria('Cmd+I', APPLE));
	assert.deepEqual(_renderedAria(APPLE), ['Escape Shift+/']);
	for (const platform of OTHERS) {
		assert.equal(triggerSsr.platformChordAria('Cmd+Shift+Z', platform), 'Control+Shift+Z');
		assert.equal(triggerSsr.assistantChordAria(platform), 'Control+I');
		const aria = _renderedAria(platform);
		assert.deepEqual(aria, ['Escape Shift+/']);
		assert.ok(!aria.some((value) => /\bMeta\b/.test(value)), `${platform} announces Meta`);
	}
});

//----- #12288: a polite live region announces the filter's result count
// KEYB-14044 (#12288): one polite live region says how many shortcuts the filter leaves.

const SEVEN = ['Load', 'Loop', 'Low', 'Lock', 'Locate', 'Cue', 'Sync'].map((word, i) => ({
	id: `row-${i}`,
	chord: String(i),
	description: `${word} the deck`,
	group: i % 2 === 0 ? 'Even' : 'Odd'
}));

/** The polite live region's text, from the overlay view rendered with `props`. */
function _renderedCount(props) {
	const body = triggerSsr.render(triggerSsr.HotkeysOverlayView, {
		props: {
			open: true,
			view: 'grid',
			query: '',
			entries: SEVEN,
			trigger: triggerSsr.HOTKEYS_OVERLAY_TRIGGER,
			onHide() {},
			onSetView() {},
			onSetQuery() {},
			...props
		}
	}).body;
	const regions = [...body.matchAll(/<p([^>]*data-hotkeys-overlay-count[^>]*)>([\s\S]*?)<\/p>/g)];
	assert.equal(regions.length, 1, 'the overlay carries exactly one result summary');
	assert.match(regions[0][1], /role="status"/);
	assert.match(regions[0][1], /aria-live="polite"/);
	const cards = (body.match(/<li class="hk-card[\s"]/g) ?? []).length;
	return { text: regions[0][2].replace(/<!--[\s\S]*?-->/g, '').trim(), cards };
}

test('if a filter query leaves five rows [then] the polite live region says 5 shortcuts found, [else stop].', () => {
	const { text, cards } = _renderedCount({ query: 'lo' });
	assert.equal(cards, 5, 'the sample query must leave five visible rows');
	assert.equal(text, '5 shortcuts found');
});

test('if a filter query leaves no rows [then] the live region says No shortcuts found, [else stop].', () => {
	assert.equal(_renderedCount({ query: 'xyzzy' }).text, 'No shortcuts found');
	assert.equal(_renderedCount({ query: '', entries: [] }).text, 'No shortcuts found');
});

test('if the filter clears [then] the live region reports every visible row, Selector controls included, [else stop].', async () => {
	assert.equal(_renderedCount({ query: '' }).text, '7 shortcuts found');
	const entries = await _overlayEntries();
	const { text, cards } = _renderedCount({ query: '  ', entries });
	assert.ok(_selectorRows(filterMod.filterHotkeys(entries, '')).length > 0);
	assert.equal(cards, entries.length, 'every entry renders as one visible row');
	assert.equal(text, `${cards} shortcuts found`);
	assert.equal(_renderedCount({ query: '', entries, view: 'list' }).text, text);
});

test('if the count is one, many, zero or invalid [then] the wording is singular, plural, none, or it throws, [else stop].', () => {
	assert.equal(filterMod.shortcutCountAnnouncement(1), '1 shortcut found');
	assert.equal(filterMod.shortcutCountAnnouncement(12), '12 shortcuts found');
	assert.equal(filterMod.shortcutCountAnnouncement(0), 'No shortcuts found');
	assert.throws(() => filterMod.shortcutCountAnnouncement(-1), /non-negative integer/);
	assert.throws(() => filterMod.shortcutCountAnnouncement(2.5), /non-negative integer/);
});

test('if a row id appears twice in the filtered rows [then] the visible count counts it once, [else stop].', () => {
	const rows = filterMod.filterHotkeys([...SEVEN, SEVEN[0]], 'load');
	assert.equal(rows.length, 2);
	assert.equal(filterMod.visibleShortcutCount(rows), 1);
	assert.equal(filterMod.visibleShortcutCount([]), 0);
});

//----- #12284: reduced motion, the overlay opens, closes and filters without animation
// Pinned through the compiler output, not source text: no Svelte transition or animate directive
// (JS-driven, which a CSS media query cannot stop) and no CSS motion of the overlay's own, so the
// app-wide A11Y-11811 rule in app.css is the only reduced-motion policy it needs.

const OVERLAY_COMPONENTS = ['HotkeysOverlay.svelte', 'HotkeysOverlayView.svelte'];
const MOTION_DECL = /(?:^|[;{\s])(?:transition|animation)(?:-[a-z-]+)?\s*:|@keyframes/;

/** Client compile of one overlay component: its generated JS and scoped CSS. */
function _compiledOverlay(file) {
	const filename = fileURLToPath(
		new URL(`../../src/lib/components/rb/hotkeys/${file}`, import.meta.url)
	);
	const out = compile(readFileSync(filename, 'utf8'), { filename, generate: 'client' });
	return { js: out.js.code, css: out.css?.code ?? '' };
}

/** The overlay view's SSR markup for `props` over the SEVEN sample rows. */
function _viewMarkup(props) {
	return triggerSsr.render(triggerSsr.HotkeysOverlayView, {
		props: {
			open: true,
			view: 'grid',
			query: '',
			entries: SEVEN,
			trigger: triggerSsr.HOTKEYS_OVERLAY_TRIGGER,
			onHide() {},
			onSetView() {},
			onSetQuery() {},
			...props
		}
	}).body;
}

test('if reduced motion is requested [then] open and close run no Svelte transition and no CSS motion, [else stop].', () => {
	for (const file of OVERLAY_COMPONENTS) {
		const { js, css } = _compiledOverlay(file);
		assert.doesNotMatch(
			js,
			/\$\.transition\(|svelte\/transition/,
			`${file} has an open/close transition`
		);
		assert.doesNotMatch(css, MOTION_DECL, `${file} styles animate`);
	}
	assert.match(_viewMarkup({}), /class="hk-backdrop[\s"]/, 'the open overlay renders');
	assert.equal(
		_viewMarkup({ open: false }).includes('hk-backdrop'),
		false,
		'closed is unmounted at once'
	);
	assert.equal(
		_viewMarkup({}).includes('motion-essential'),
		false,
		'the global rule must reach every node'
	);
});

test('if reduced motion is not requested [then] the overlay carries no motion-conditional rule of its own, [else stop].', () => {
	for (const file of OVERLAY_COMPONENTS) {
		const { css } = _compiledOverlay(file);
		assert.doesNotMatch(
			css,
			/prefers-reduced-motion|data-reduce-motion/,
			`${file} branches on motion`
		);
	}
	const view = _compiledOverlay('HotkeysOverlayView.svelte').css;
	for (const rule of ['.hk-backdrop', '.hk-panel', '.hk-card', '.hk-mark']) {
		assert.ok(view.includes(rule), `${rule} keeps its presentation rule`);
	}
});

test('if the overlay is filtered under reduced motion [then] result rows swap with no animate or row transition, [else stop].', () => {
	const { js } = _compiledOverlay('HotkeysOverlayView.svelte');
	assert.doesNotMatch(js, /\$\.animation\(|svelte\/animate/, 'filtered rows animate');
	const cards = (markup) => (markup.match(/<li class="hk-card[\s"]/g) ?? []).length;
	assert.equal(cards(_viewMarkup({ query: '' })), 7);
	assert.equal(cards(_viewMarkup({ query: 'lo' })), 5, 'the filter swaps rows in one render');
});
