/**
 * requirement: CHROME-07, LIBM-29
 *
 * The MIDI drawer and the track-row popovers only exist after a click, so
 * they are fetched by that click instead of riding in the /performance boot
 * bundle (issue #3886 put `performance` over its budget; deferring these paid
 * it back). These checks read the source because the property under test is
 * the import SHAPE: a static import anywhere on the boot path would silently
 * put the bytes back, and the bundle gate only notices once it is over.
 *
 * Regression lines:
 *   if TopBar statically imports MidiPanel.svelte then broken
 *   if MidiPanelLoader imports MidiPanel.svelte other than by import() then broken
 *   if a failed MidiPanel import renders no role="alert" then broken
 *   if TrackTable statically imports RelocatePopover or TrackPlaylistsPopover then broken
 *   if TrackRowPopovers fetches either popover other than by import() then broken
 *   if a failed popover import renders no role="alert" then broken
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = (rel) => fs.readFileSync(path.join(FRONTEND_ROOT, rel), 'utf8');

const topBar = read('src/lib/components/rb/TopBar.svelte');
const midiLoader = read('src/lib/components/rb/midi/MidiPanelLoader.svelte');
const trackTable = read('src/lib/components/rb/browser/TrackTable.svelte');
const rowPopovers = read('src/lib/components/rb/browser/TrackRowPopovers.svelte');

// A static import is `import X from '...Y.svelte'`; import('...') is dynamic.
const staticImportOf = (file) => new RegExp(`import\\s+\\w+\\s+from\\s+['"][^'"]*${file}['"]`);
const dynamicImportOf = (file) => new RegExp(`import\\(\\s*['"][^'"]*${file}['"]\\s*\\)`);

test('TopBar mounts the MIDI drawer through the lazy loader, never statically', () => {
	assert.doesNotMatch(topBar, staticImportOf('MidiPanel\\.svelte'));
	assert.match(topBar, staticImportOf('midi/MidiPanelLoader\\.svelte'));
	assert.match(topBar, /<MidiPanelLoader \/>/);
});

test('the MIDI drawer module is fetched by the open flip, and a failure is shown inline', () => {
	assert.doesNotMatch(midiLoader, staticImportOf('MidiPanel\\.svelte'));
	assert.match(midiLoader, dynamicImportOf('MidiPanel\\.svelte'));
	assert.match(midiLoader, /midiUi\.panelOpen/);
	assert.match(midiLoader, /\.catch\(/);
	assert.match(midiLoader, /role="alert"/);
	assert.match(midiLoader, /MIDI panel failed to load: \{loadError\}/);
});

test('TrackTable mounts the row popovers through the lazy wrapper, never statically', () => {
	for (const file of ['RelocatePopover\\.svelte', 'TrackPlaylistsPopover\\.svelte']) {
		assert.doesNotMatch(trackTable, staticImportOf(file));
	}
	assert.match(trackTable, /<TrackRowPopovers bind:playlistsMenu bind:relocateMenu \{onrelocated\} \/>/);
});

test('each row popover is fetched by the pick that opens it, and a failure is shown inline', () => {
	for (const file of ['RelocatePopover\\.svelte', 'TrackPlaylistsPopover\\.svelte']) {
		assert.doesNotMatch(rowPopovers, staticImportOf(file));
		assert.match(rowPopovers, dynamicImportOf(file));
	}
	assert.equal((rowPopovers.match(/\.catch\(/g) ?? []).length, 2);
	assert.equal((rowPopovers.match(/role="alert"/g) ?? []).length, 2);
	assert.match(rowPopovers, /Show in playlists failed to load: \{playlistsError\}/);
	assert.match(rowPopovers, /Relocate failed to load: \{relocateError\}/);
});
