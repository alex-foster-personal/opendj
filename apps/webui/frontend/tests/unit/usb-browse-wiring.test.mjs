// requirement: USBPLAY-02, USBPLAY-05, USBPLAY-09
/**
 * Play from USB wiring that node cannot render: the stick list, the lazy
 * stick tree and BrowserPanel's stick pane branch, checked at the source the
 * same way library-source-tabs.test.mjs checks the taglist branch. The
 * behavior behind each branch is covered by usb-library-store.test.mjs and
 * usb-row-wire.test.mjs; these checks pin that the branches are wired.
 *
 * Regression lines:
 * - [if] the stick list, tree or store is imported statically from
 *   LibraryNav or BrowserPanel [then] the stick code rides in the
 *   /performance first-paint bundle
 * - [if] a denied stick shows no settings link [then] the DJ has no way out
 * - [if] a stick pane is written to last_playlist [then] the next boot opens a
 *   pane for a stick that may be gone
 * - [if] rating a stick row reaches _patchRating [then] the app pretends to
 *   write to a read-only stick
 * - [if] stick rows are sent to rb-meta [then] every visible row 404s
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

function between(text, start, end) {
	const from = text.indexOf(start);
	assert.ok(from >= 0, `missing start marker: ${start}`);
	const to = text.indexOf(end, from + start.length);
	assert.ok(to > from, `missing end marker after ${start}: ${end}`);
	return text.slice(from, to);
}

const STATIC_IMPORT = (what) => new RegExp(`^\\s*import\\s[^;]*['"][^'"]*${what}['"]`, 'm');

test('the USBs tab body is lazy: LibraryNav imports the stick list only on demand', () => {
	const nav = source('src/lib/components/rb/browser/LibraryNav.svelte');
	assert.match(nav, /import\('\.\/UsbSourceList\.svelte'\)/);
	assert.doesNotMatch(nav, STATIC_IMPORT('\\./UsbSourceList(\\.svelte)?'));
	assert.doesNotMatch(nav, /UsbStickTree|usb-library/);
	// Control: the pattern does see a static import that exists.
	assert.match(nav, STATIC_IMPORT('\\./TaglistTree\\.svelte'));
	// The import is armed from the tab itself, so it runs on the first visit.
	assert.match(nav, /else if \(activeTab === 'usbs'\) loadUsbList\(\);/);
	// The tree lives inside that lazy chunk, and the list never reads the
	// stick store itself: only an opened stick's tree does.
	const list = source('src/lib/components/rb/browser/UsbSourceList.svelte');
	assert.match(list, STATIC_IMPORT('\\./UsbStickTree\\.svelte'));
	assert.doesNotMatch(list, /usb-library/);
	assert.match(list, /\{:else if openSticks\[vol\.id\]\}\s*<UsbStickTree /);
});

test('BrowserPanel reaches the stick store only through the lazy barrel', () => {
	const panel = source('src/lib/components/rb/BrowserPanel.svelte');
	const support = source('src/lib/components/rb/browser/browser-panel-support.ts');
	assert.doesNotMatch(panel, /usb-library/);
	assert.match(support, /return import\('\$lib\/rb\/usb-library\.svelte'\);/);
	assert.doesNotMatch(support, STATIC_IMPORT('\\$lib/rb/usb-library(\\.svelte)?'));
});

test('stick rows show both permission states, with a way to System Settings', () => {
	const list = source('src/lib/components/rb/browser/UsbSourceList.svelte');
	const pending = between(list, "{#if vol.access === 'pending'}", "{:else if vol.access === 'denied'}");
	assert.match(pending, /waiting for permission/);
	assert.match(pending, /title=/, 'the pending state explains itself on hover');
	const denied = between(list, "{:else if vol.access === 'denied'}", '{:else if openSticks');
	assert.match(denied, /Open DJ cannot read this drive/);
	assert.match(denied, /href=\{FILES_AND_FOLDERS_URL\}/);
	assert.match(
		list,
		/x-apple\.systempreferences:com\.apple\.preference\.security\?Privacy_FilesAndFolders/
	);
	// A blocked stick does not open (there is nothing it could read).
	assert.match(between(list, 'function _toggleStick', '}\n'), /if \(usbAccessBlocked\(vol\)\) return;/);
});

test('LibraryNav hands the stick list the tree selection props', () => {
	const nav = source('src/lib/components/rb/browser/LibraryNav.svelte');
	assert.match(
		nav,
		/<UsbListView\s+selectedId=\{playlistTreeProps\.selectedId\}\s+onselect=\{playlistTreeProps\.onselect\}\s*\/>/
	);
	// A failed import says so in place, never a blank tab.
	assert.match(nav, /data-testid="usb-list-failed"/);
});

test('a stick node is its own PlaylistNode and pane kind', () => {
	const types = source('src/lib/rb/library-types.ts');
	assert.match(between(types, 'kind:', ';'), /\| 'usb'/);
	const contract = source('src/lib/components/rb/browser/pane-contract.svelte.ts');
	assert.match(contract, /kind = \$state<PlaylistNode\['kind'\] \| null>\(null\);/);
});

test('_loadPane keeps stick panes out of last_playlist and loads them from the stick', () => {
	const panel = source('src/lib/components/rb/BrowserPanel.svelte');
	const load = between(panel, 'async function _loadPane(', '/** Reconstructs the minimal PlaylistNode');
	const persisted = between(load, 'if (', 'setLastPlaylist(');
	assert.match(persisted, /node\.kind !== 'usb'/);
	// Control: the slice is the exclusion list (taglists are excluded too).
	assert.match(persisted, /node\.kind !== 'taglist'/);
	assert.match(
		between(load, "if (node.kind === 'usb') {", 'return;'),
		/loadUsbPane\(p, seq, \(\) => panes\)/
	);
});

test('stick panes refresh from the stick and navigate by their usbpl: prefix', () => {
	const panel = source('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(panel, /if \(p\.kind === 'usb'\) \{\s*await \(await usbPaneSource\(\)\)\.refreshUsbPane\(p\);\s*continue;/);
	const nav = between(panel, 'function _nodeForNav(', 'async function goBack(');
	assert.match(nav, /startsWith\('usbpl:'\)\s*\?\s*'usb'/);
	const current = between(panel, 'function _currentNode(', '\n\t}\n');
	assert.match(current, /p\.kind === 'usb'/);
});

test('stick rows are read only and skip rb-meta hydration', () => {
	const panel = source('src/lib/components/rb/BrowserPanel.svelte');
	const rate = between(panel, 'function rateRow(', 'void _patchRating(row, next);');
	assert.match(rate, /stable_id\.startsWith\('usb-'\)[\s\S]*read only[\s\S]*return;/);
	const hydrate = between(panel, 'row.rb_meta !== null', '_inflight.add(row.stable_id);');
	assert.match(hydrate, /row\.stable_id\.startsWith\('usb-'\)/);
});

test('every numeric readout in the stick tree carries a hover title', () => {
	const tree = source('src/lib/components/rb/browser/UsbStickTree.svelte');
	const count = between(tree, 'class="count"', '</span');
	assert.match(count, /title=\{/);
	assert.match(count, /tracks in this list on the stick/);
});
