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
 *   if any lazy-load error promises a same-document retry, lacks Reload, or
 *     reloads without a click then broken
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
const performancePage = read('src/routes/performance/+page.svelte');
const quickDrawLoader = read('src/lib/components/rb/QuickDrawMenuLoader.svelte');
const quickDrawMenu = read('src/lib/components/rb/QuickDrawMenu.svelte');

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
// shown, recovered by Reload) is tests/e2e/lazy-chunk-reload-recovery.spec.ts;
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

// Codex P2 on PR #3896 (comment 4130011276): a failed import() stays failed in
// its document (the browser's module map keeps it), so the only recovery is a
// fresh document the user asks for. The end-to-end proof, including the
// same-document negative control, is tests/e2e/lazy-chunk-reload-recovery.spec.ts.
test('every lazy-load error offers Reload on click, and none promises a same-document retry', () => {
	for (const [src, errors] of [
		[midiLoader, 1],
		[midiPanel, 2],
		[rowPopovers, 2]
	]) {
		// The markup is what the user reads; comments may describe the old promise.
		const markup = src.slice(src.lastIndexOf('</script>'));
		assert.match(markup, /failed to load/);
		assert.doesNotMatch(markup, /reopen|retry/i);
		const reloads = src.match(/location\.reload\(\)/g) ?? [];
		const onClick = src.match(/onclick=\{\(\) => location\.reload\(\)\}>Reload</g) ?? [];
		assert.equal(onClick.length, errors, 'one Reload button per error surface');
		assert.equal(reloads.length, onClick.length, 'location.reload() only inside a Reload click');
	}
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

test('/performance mounts the quick-draw menu through the lazy loader, never statically', () => {
	assert.doesNotMatch(performancePage, staticImportOf('QuickDrawMenu\\.svelte'));
	assert.match(performancePage, staticImportOf('QuickDrawMenuLoader\\.svelte'));
	assert.match(performancePage, /<QuickDrawMenuLoader \/>/);
});

test('the quick-draw module is fetched by a .perf-root right-click, and a failure is shown inline', () => {
	assert.doesNotMatch(quickDrawLoader, staticImportOf('QuickDrawMenu\\.svelte'));
	assert.match(quickDrawLoader, dynamicImportOf('QuickDrawMenu\\.svelte'));
	assert.match(quickDrawLoader, /<svelte:window oncontextmenu=\{onContextMenu\} \/>/);
	assert.match(quickDrawLoader, /closest\('\.perf-root'\)/);
	assert.match(quickDrawLoader, /\.catch\(/);
	assert.equal((quickDrawLoader.match(/role="alert"/g) ?? []).length, 1);
	assert.match(quickDrawLoader, /Quick-draw menu failed to load: \{loadError\}/);
	assert.doesNotMatch(quickDrawLoader, /pushToast/, 'the failure is inline, never a toast');
});

test('the right-click that fetched the menu is the one it opens for', () => {
	assert.match(quickDrawLoader, /<QuickDrawMenuComponent initialEvent=\{pendingEvent\} \/>/);
	assert.match(quickDrawMenu, /let \{ initialEvent = null \}: \{ initialEvent\?: MouseEvent \| null \} = \$props\(\);/);
	assert.match(quickDrawMenu, /if \(initialEvent !== null\) onContextMenu\(initialEvent\);/);
});

// A failed import() stays failed in its document (the browser's module map
// keeps it), so the only recovery is a fresh document the user asks for.
test('the lazy-load error offers Reload on click, and never promises a same-document retry', () => {
	// The markup is what the user reads; comments may describe the old promise.
	const markup = quickDrawLoader.slice(quickDrawLoader.lastIndexOf('</script>'));
	assert.match(markup, /failed to load/);
	assert.doesNotMatch(markup, /reopen|retry/i);
	const reloads = quickDrawLoader.match(/location\.reload\(\)/g) ?? [];
	const onClick = quickDrawLoader.match(/onclick=\{\(\) => location\.reload\(\)\}>Reload</g) ?? [];
	assert.equal(onClick.length, 1, 'one Reload button on the error surface');
	assert.equal(reloads.length, onClick.length, 'location.reload() only inside a Reload click');
});
