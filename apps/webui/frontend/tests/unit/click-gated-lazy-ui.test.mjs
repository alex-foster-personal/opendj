/**
 * requirement: CHROME-07, LIBM-29, UX-FLOAT-01
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
 *   if TopBar imports the learn-log pop-out at all (it lives in MidiPanel) then broken
 *   if MidiPanel's device-list or pop-out import has no .catch then broken
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
const midiPanel = read('src/lib/components/rb/MidiPanel.svelte');
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

// Codex P2 on PR #3896 (comment 4129741948). The browser behavior (error
// shown, retried on the next open) is tests/e2e/midi-panel-lazy-load-errors.spec.ts;
// this pins the shape that keeps the pop-out out of TopBar's boot chunk.
test('the learn-log pop-out and device list load inside MidiPanel, each with a handled failure', () => {
	assert.doesNotMatch(topBar, /MidiLearnLogPopout/);
	for (const file of ['MidiDeviceList\\.svelte', 'MidiLearnLogPopout\\.svelte']) {
		assert.doesNotMatch(midiPanel, staticImportOf(file));
		assert.match(midiPanel, dynamicImportOf(file));
	}
	assert.equal((midiPanel.match(/\.catch\(/g) ?? []).length, 2);
	assert.match(midiPanel, /Devices failed to load: \{deviceListError\}/);
	assert.match(midiPanel, /Learn log pop-out failed to load: \{popoutError\}/);
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

// Codex P2 on PR #3896: the load-error boxes used the raw pointer point with
// position: fixed, so near the bottom or right edge the message and its Close
// button landed offscreen. They must go through the same viewport-clamping
// action the loaded popovers use.
test('each popover load-error box is placed by pointFloatingAction at its own anchor, never raw coordinates', () => {
	assert.match(rowPopovers, /import \{ pointFloatingAction \} from '\$lib\/ui\/clamp-to-viewport'/);
	const boxes = [...rowPopovers.matchAll(/<div\s+class="popover-load-error"[^>]*>/g)].map((m) => m[0]);
	assert.equal(boxes.length, 2, 'one error box per popover');
	assert.match(boxes[0], /use:pointFloatingAction=\{\{ x: playlistsMenu\.x, y: playlistsMenu\.y \}\}/);
	assert.match(boxes[1], /use:pointFloatingAction=\{\{ x: relocateMenu\.x, y: relocateMenu\.y \}\}/);
	for (const box of boxes) assert.doesNotMatch(box, /style=/, 'raw left/top would bypass the clamp');
	// The loaded popovers use the same action, so both paths clamp alike.
	for (const loaded of ['TrackPlaylistsPopover.svelte', 'RelocatePopover.svelte']) {
		assert.match(read(`src/lib/components/rb/browser/${loaded}`), /use:pointFloatingAction=\{\{ x, y \}\}/);
	}
});
