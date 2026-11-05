// LIBUX tabs: the library browser tab model (close guard, shortcuts, persist/restore).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const mod = await loadTypeScriptModule('src/lib/rb/library-tabs.ts');

const pane = (playlist_id, title, extra = {}) => ({
	playlist_id,
	title,
	search: '',
	sort_key: null,
	sort_dir: 1,
	...extra
});

test('close: last tab cannot close (null), closing before or at the end moves active left', () => {
	assert.equal(mod.activeAfterClose(1, 0, 0), null);
	assert.equal(mod.activeAfterClose(3, 2, 0), 1);
	assert.equal(mod.activeAfterClose(3, 2, 2), 1);
	assert.equal(mod.activeAfterClose(3, 0, 2), 0);
	assert.equal(mod.activeAfterClose(3, 1, 1), 1);
});

test('switch: Cmd+Shift+] / [ cycle and wrap', () => {
	assert.equal(mod.cycleTab(3, 2, 1), 0);
	assert.equal(mod.cycleTab(3, 0, -1), 2);
	assert.equal(mod.cycleTab(1, 0, 1), 0);
});

test('shortcuts: Cmd/Ctrl+T new, +W close, +Shift+[ / ] prev/next; plain keys ignored', () => {
	const k = (key, o = {}) => ({ key, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, ...o });
	assert.equal(mod.libraryTabShortcut(k('t', { metaKey: true })), 'new');
	assert.equal(mod.libraryTabShortcut(k('w', { ctrlKey: true })), 'close');
	assert.equal(mod.libraryTabShortcut(k('}', { metaKey: true, shiftKey: true })), 'next');
	assert.equal(mod.libraryTabShortcut(k(']', { metaKey: true, shiftKey: true })), 'next');
	assert.equal(mod.libraryTabShortcut(k('{', { metaKey: true, shiftKey: true })), 'prev');
	assert.equal(mod.libraryTabShortcut(k('t')), null);
	assert.equal(mod.libraryTabShortcut(k('t', { metaKey: true, altKey: true })), null);
});

test('persist: per-tab list, search and sort serialize; blank list keeps null id', () => {
	const pref = mod.serializeLibraryTabs(
		[pane('all', 'All Tracks', { search: 'house', sort_key: 'bpm', sort_dir: -1 }), pane(null, 'blank list')],
		1
	);
	assert.deepEqual(pref, {
		tabs: [
			{ playlist_id: 'all', name: 'All Tracks', search: 'house', sort_key: 'bpm', sort_dir: -1 },
			{ playlist_id: null, name: 'blank list', search: '', sort_key: null, sort_dir: 1 }
		],
		active: 1
	});
});

test('restore: equal prefs compare equal, any per-tab change or active change differs', () => {
	const a = mod.serializeLibraryTabs([pane('all', 'All'), pane('p1', 'Warmup')], 0);
	assert.ok(mod.sameLibraryTabs(a, structuredClone(a)));
	assert.ok(!mod.sameLibraryTabs(a, { ...a, active: 1 }));
	const b = structuredClone(a);
	b.tabs[1].sort_key = 'key';
	assert.ok(!mod.sameLibraryTabs(a, b));
});

test('restore: an unknown playlist is dropped, active stays on a real tab, all-unknown restores nothing', () => {
	const pref = mod.serializeLibraryTabs([pane('gone', 'Gone'), pane('p1', 'Warmup'), pane('p2', 'Peak')], 2);
	const known = new Set(['all', 'p1', 'p2']);
	const plan = mod.restorableLibraryTabs(pref, (id) => known.has(id));
	assert.deepEqual(plan.tabs.map((t) => t.playlist_id), ['p1', 'p2']);
	assert.equal(plan.active, 1);
	assert.equal(mod.restorableLibraryTabs({ tabs: [pane('gone', 'Gone')], active: 0 }, () => false), null);
	assert.equal(mod.restorableLibraryTabs({ tabs: [], active: 0 }, () => true), null);
});
