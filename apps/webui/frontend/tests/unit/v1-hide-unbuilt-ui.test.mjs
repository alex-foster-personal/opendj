/**
 * V1 polish, "hide unbuilt UI" (JIK, Thu 1 Oct 2026): unbuilt controls are
 * HIDDEN rather than shown inert, developer pages and unbuilt settings sit
 * behind one "Show developer pages" pref (show_dev_ui), and the top bar's
 * 2-deck button is a real toggle on the Cmd+2 / Cmd+4 code path.
 *
 * Each test asserts the PRESENCE of the new behavior (the toggle's pressed
 * state and next mode, the dev pages appearing when the flag is on, the todo
 * rows appearing when it is on), not only the absence of the old thing.
 *
 * [if] the 2-deck button does not flip deck_layout via setDeckLayoutMode [then] broken
 * [if] show_dev_ui=false still lists Admin/Progress/Queues or todo rows [then] broken
 * [if] show_dev_ui=true does NOT bring them back [then] the flag is a dead end - broken
 * [if] a header count's title is just the number again [then] broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (rel) =>
	readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');

/** The registry's track contributions: from `const TRACK_MENU` to the next top-level item. */
function trackMenuSource() {
	const src = read('src/lib/context-menu/registry.ts');
	const start = src.indexOf('const TRACK_MENU');
	assert.ok(start >= 0, 'TRACK_MENU not found in the registry');
	return src.slice(start, src.indexOf('\n/**', start));
}

// ------------------------------------------------------------ item 1

test('RecommendedSection renders no "not implemented" placeholder, but still renders paired rows', () => {
	const src = read('src/lib/components/rb/RecommendedSection.svelte');
	assert.doesNotMatch(src, /Recommended: not implemented/);
	assert.doesNotMatch(src, /rec-placeholder/);
	// The real half of the section is still there.
	assert.match(src, /class="rec-paired"/);
	assert.match(src, /listPairingsFor\(sid\)/);
});

// ------------------------------------------------------------ item 2

test('describeTwoDeckToggle: pressed in LESS, next flips, title names both shortcuts', async () => {
	const { describeTwoDeckToggle } = await loadTypeScriptModule('src/lib/rb/two-deck-toggle.ts');
	const less = describeTwoDeckToggle('less');
	assert.equal(less.pressed, true);
	assert.equal(less.next, 'more');
	const more = describeTwoDeckToggle('more');
	assert.equal(more.pressed, false);
	assert.equal(more.next, 'less');
	for (const v of [less, more]) {
		assert.match(v.title, /Cmd\+2/);
		assert.match(v.title, /Cmd\+4/);
	}
	assert.notEqual(less.title, more.title, 'the title must say which state is live');
});

test('TopBar 2-deck button is live, pressed-state aware, and on the hotkey setter path', () => {
	const topbar = read('src/lib/components/rb/TopBar.svelte');
	const hotkeys = read('src/lib/rb/deck-layout-hotkeys.ts');
	const btn = topbar.match(/<button(?:(?!<button)[\s\S])*?aria-label="2 deck view"[\s\S]*?<\/button>/)?.[0];
	assert.ok(btn, '2-deck button is rendered');
	assert.doesNotMatch(btn, /\sdisabled(?:\s|\{|>|=|$)/, 'must not be a disabled control');
	assert.doesNotMatch(btn, /rb-inert/);
	assert.match(btn, /aria-pressed=\{twoDeck\.pressed\}/);
	assert.match(btn, /aria-keyshortcuts="Meta\+2 Control\+2 Meta\+4 Control\+4"/);
	assert.doesNotMatch(btn, /\btitle=/, '2-deck uses ControlExplainer only (issue #927)');
	assert.match(btn, /onclick=\{\(\) => setDeckLayoutMode\(twoDeck\.next\)\}/);
	assert.match(topbar, /describeTwoDeckToggle\(uiPrefs\.deck_layout\)/);
	// Same setter the Cmd+2 / Cmd+4 handler calls.
	assert.match(hotkeys, /setDeckLayoutMode\(mode\)/);
	// Unbuilt neighbors are hidden, not disabled.
	assert.doesNotMatch(topbar, /aria-label="split view"/);
	assert.doesNotMatch(topbar, /aria-label="grid view"/);
});

// ------------------------------------------------------------ item 3

test('TrackContextMenu keeps real rows and drops the inert unbuilt ones', () => {
	// W28: the rows live in the registry's track contributions.
	const src = trackMenuSource();
	for (const id of ['edit', 'stems-open', 'offline', 'cloud-only']) {
		assert.doesNotMatch(src, new RegExp(`id: '${id}'`), `${id} row should be hidden`);
	}
	for (const id of ['finder', 'copy-path', 'analyze', 'stems-generate', 'lyrics', 'remove-playlist']) {
		assert.match(src, new RegExp(`id: '${id}'`), `${id} row should stay`);
	}
	assert.match(src, /id: 'remove-playlist',[\s\S]*?when: \(ctx\) => ctx\.source === 'playlist'/);
	const host = read('src/lib/components/rb/browser/TrackContextMenu.svelte');
	assert.match(host, /source: isUsbTrackId\(row\.stable_id\)\s*\?\s*'usb'\s*:\s*readOnly\s*\?\s*'readonly-import'\s*:\s*removable\s*\?\s*'playlist'\s*:\s*'collection'/);
});

test('TreeContextMenu keeps real rows and drops the inert unbuilt ones', () => {
	// W28: the rows live in the registry's tree contributions.
	const src = read('src/lib/context-menu/registry.ts');
	for (const id of ['new-folder', 'export', 'spotify', 'offline', 'sort']) {
		assert.doesNotMatch(src, new RegExp(`id: '${id}'`), `${id} row should be hidden`);
	}
	// forbid-duplicates appears exactly once: the live playlist checkbox,
	// with no inert placeholder for folders/smartlists.
	assert.equal(src.match(/id: 'forbid-duplicates'/g)?.length, 1);
	const host = read('src/lib/components/rb/browser/TreeContextMenu.svelte');
	assert.match(host, /'forbid-duplicates': \(\) => onforbidduplicates\?\.\(node\)/);
	for (const id of ['new-playlist', 'new-smartlist', 'rename', 'delete', 'duplicate', 'reveal']) {
		assert.match(src, new RegExp(`id: '${id}'`), `${id} row should stay`);
	}
});

// ------------------------------------------------------------ item 4

test('show_dev_ui pref defaults off, accepts booleans, rejects junk, and its setter persists', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/dev-ui-prefs.ts');
	assert.equal(mod.DEV_UI_PREF_DEFAULTS.show_dev_ui, false);
	assert.deepEqual(mod.mergeDevUiPrefsFromParsed({}, 'k'), { show_dev_ui: false });
	assert.deepEqual(mod.mergeDevUiPrefsFromParsed({ show_dev_ui: true }, 'k'), { show_dev_ui: true });
	assert.throws(() => mod.mergeDevUiPrefsFromParsed({ show_dev_ui: 'yes' }, 'k'), /show_dev_ui/);
	const state = { show_dev_ui: false };
	let persisted = 0;
	const { setShowDevUi } = mod.makeDevUiPrefSetters(state, () => persisted++);
	setShowDevUi(true);
	assert.equal(state.show_dev_ui, true);
	assert.equal(persisted, 1);
});

test('a todo settings row is hidden by default and revealed by show_dev_ui', async () => {
	const search = await loadTypeScriptModule('src/lib/settings/search.ts');
	// The shipped catalog has no todo row (SETSTUB-01), so the visibility rule
	// is pinned on an explicit in-test fixture row, never a production stub.
	const TODO_ROW = {
		id: 'fixture.todo',
		label: 'Fixture todo',
		group: 'advanced',
		keywords: [],
		title: 'not implemented - see PARITY-TODO',
		detail: 'fixture',
		implemented: false,
		control: { kind: 'boolean' }
	};
	const BUILT_ROW = { ...TODO_ROW, id: 'fixture.built', implemented: true };
	const visible = (prefs, showDev, row) =>
		search.settingRowVisible(row, { hideTodo: search.effectiveHideTodo(prefs), showDev });

	// Default (dev off): the todo row is hidden, a built row is not.
	const off = { show_dev_ui: false, hide_todo_settings: false };
	assert.equal(visible(off, false, TODO_ROW), false);
	assert.equal(visible(off, false, BUILT_ROW), true, 'control: a built row stays visible');
	// Dev on: the todo row comes back.
	const on = { show_dev_ui: true, hide_todo_settings: false };
	assert.equal(visible(on, true, TODO_ROW), true);
	// Dev on + "Hide todo" on: hidden again.
	const both = { show_dev_ui: true, hide_todo_settings: true };
	assert.equal(visible(both, true, TODO_ROW), false);
	assert.equal(visible(both, true, BUILT_ROW), true);

	// The real catalog: dev-only rows follow the flag, and the flag itself is
	// always reachable so it can be turned back off.
	const offRows = search.filterSettings('', { hideTodo: search.effectiveHideTodo(off), showDev: false, group: null }).all;
	assert.ok(!offRows.some((s) => s.id === 'hide_todo_settings'));
	assert.ok(offRows.some((s) => s.id === 'show_dev_ui'), 'the flag itself is reachable');
	const onRows = search.filterSettings('', { hideTodo: search.effectiveHideTodo(on), showDev: true, group: null }).all;
	assert.ok(onRows.some((s) => s.id === 'hide_todo_settings'));
	const bothRows = search.filterSettings('', { hideTodo: search.effectiveHideTodo(both), showDev: true, group: null }).all;
	assert.ok(bothRows.some((s) => s.id === 'hide_todo_settings'));
	// No "(todo)" parity groups in either mode (SETSTUB-01).
	for (const mode of [{ hideTodo: true, showDev: false }, { hideTodo: false, showDev: true }]) {
		const groups = search.visibleGroups('', mode);
		assert.ok(groups.includes('performance'), 'control: groups were computed');
		assert.ok(!groups.includes('rekordbox') && !groups.includes('djay'));
	}
});

test('show_dev_ui is an allowlisted, readable setting the overlay can toggle', () => {
	const apply = read('src/lib/settings/apply.ts');
	assert.match(apply, /'show_dev_ui',/);
	assert.match(apply, /case 'show_dev_ui':\s*return uiPrefs\.show_dev_ui;/);
	assert.match(apply, /case 'show_dev_ui':\s*setShowDevUi\(_asBool\(value, key\)\);/);
	const overlay = read('src/lib/components/settings/SettingsOverlay.svelte');
	assert.match(overlay, /effectiveHideTodo\(uiPrefs\)/);
	assert.match(overlay, /showDev,/);
});

// ------------------------------------------------------------ item 5

test('sidebar: dev pages only with show_dev_ui, one Settings entry', async () => {
	const nav = await loadTypeScriptModule('src/lib/shell/sidebar-nav.ts');
	const hrefs = (showDev) => nav.sidebarNavLinks(showDev).map((l) => l.href);
	const off = hrefs(false);
	for (const dev of ['/admin', '/progress-tree', '/queues']) {
		assert.ok(!off.includes(dev), `${dev} hidden by default`);
		assert.ok(hrefs(true).includes(dev), `${dev} listed with show_dev_ui`);
	}
	assert.ok(off.includes('/'), 'control: ordinary pages still listed');
	const settings = nav.sidebarNavLinks(true).filter((l) => /settings/i.test(l.label));
	assert.equal(settings.length, 1);
	assert.equal(settings[0].href, '/settings');
	assert.match(settings[0].title, /Cmd\+,/);
});

test('isCurrentNavLink marks the page on screen and only it', async () => {
	const { isCurrentNavLink } = await loadTypeScriptModule('src/lib/shell/sidebar-nav.ts');
	assert.equal(isCurrentNavLink('/', '/'), true);
	assert.equal(isCurrentNavLink('/', '/pairings'), false);
	assert.equal(isCurrentNavLink('/settings', '/settings'), true);
	assert.equal(isCurrentNavLink('/settings', '/settings/x'), true);
	assert.equal(isCurrentNavLink('/sets', '/settings'), false);
});

test('layout renders nav from the shared list with aria-current and a visible style', () => {
	const layout = read('src/routes/+layout.svelte');
	assert.match(layout, /sidebarNavLinks\(uiPrefs\.show_dev_ui\)/);
	assert.match(layout, /aria-current=\{current \? 'page' : undefined\}/);
	assert.match(layout, /\.sidebar nav a\[aria-current='page'\]/);
	assert.doesNotMatch(layout, /Settings \(daemon\)/);
	assert.doesNotMatch(layout, /Settings \(Cmd\+,\)/);
});

test('header counts explain what they count instead of repeating the number', async () => {
	const nav = await loadTypeScriptModule('src/lib/shell/sidebar-nav.ts');
	assert.match(nav.headerTrackCountTitle(8355), /^8355 tracks: .*library database/);
	assert.match(nav.headerPlaylistCountTitle(12), /^12 playlists: .*not counting deleted/);
	const layout = read('src/routes/+layout.svelte');
	assert.match(
		layout,
		/title=\{headerTrackCountTitle\(health\.data\.state_db\.tracks, health\.data\.state_db\.tracks_playable \?\? 0\)\}/
	);
	assert.match(layout, /playable\)/);
	assert.match(layout, /title=\{headerPlaylistCountTitle\(health\.data\.state_db\.playlists\)\}/);
	assert.doesNotMatch(layout, /title=\{String\(health\.data\.state_db/);
});
