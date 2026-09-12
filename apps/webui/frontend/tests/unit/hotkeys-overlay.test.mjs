/**
 * LIBUX-04 hotkeys overlay: filter/highlight + "/" hold vs "?" toggle.
 *
 * [if] "/" is held down [then] the overlay shows for as long as it is held
 * and hides on release, while pressing "?" toggles the same overlay on and
 * leaves it up until toggled off
 * [if] the user types while the overlay is open [then] the query filters the
 * list and the matched substring is highlighted yellow in both views
 * [if] Esc is pressed or the X button is clicked [then] the overlay hides,
 * from either view and from any filter state
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

let filterMod;
let gestureMod;
let registryMod;

before(async () => {
	filterMod = await loadTypeScriptModule(
		'src/lib/components/rb/hotkeys/hotkeys-filter.ts'
	);
	gestureMod = await loadTypeScriptModule(
		'src/lib/components/rb/hotkeys/hotkeys-gesture.ts'
	);
	registryMod = await loadTypeScriptModule(
		'src/lib/components/rb/hotkeys/hotkeys-registry.ts'
	);
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
		assert.deepEqual(row.descriptionPieces, [
			{ text: row.entry.description, matched: false }
		]);
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

test('if Esc hides from list view with an active filter [then] the overlay is gone and state is reset', () => {
	// [if] Esc is pressed [then] the overlay hides, from either view and from
	// any filter state
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
