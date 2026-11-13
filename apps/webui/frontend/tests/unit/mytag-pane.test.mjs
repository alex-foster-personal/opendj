/**
 * Issue #10026: the My Tag pane docked right of the track table (rekordbox My Tag pane, mimicked).
 *
 * The reducer and the write are pure; the pane renders through Svelte SSR, so the click, the dock layout and
 * the row refresh after a write are not exercised (e2e not run).
 *
 * [if] tags are not grouped by type with untyped ones under Other [then] the pane is not the My Tag pane - broken.
 * [if] a tag held by some selected tracks does not read mixed [then] a tick silently overwrites the others - broken.
 * [if] a click does not add to (or take off) every selected track [then] the selection and the tags disagree - broken.
 * [if] the pane shows a blank column with nothing selected [then] it reads as broken - broken.
 * [if] the toggle is not a persisted pref with a performance command twin [then] agents and reloads lose it - broken.
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

// The engine reads the page URL for ?extroute=; these tests run as a page with no query.
globalThis.location ??= { search: '' };

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

const tagType = (id, label, sortIndex) => ({
	id,
	label,
	singular: label,
	sort_index: sortIndex,
	show_as_column: 0,
	show_in_tags: 1,
	column_width: null
});
const TYPES = [
	tagType('general', 'General', 0),
	tagType('mood', 'Mood', 2),
	tagType('genre', 'Genre', 1)
];
const tag = (name, type, color = null) => ({
	name,
	name_norm: name.toLowerCase(),
	type,
	color,
	track_count: 1
});
const CATALOG = [
	tag('Dark', 'mood', '#333333'),
	tag('Warmup', 'general'),
	tag('House', 'genre'),
	tag('Odd', 'gone-type')
];
const ASSIGNMENTS = { a: ['dark', 'House'], b: ['House'], c: [] };

let state;
let ssr;

before(async () => {
	state = await loadTypeScriptModule('src/lib/components/rb/browser/mytag-pane-state.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as MyTagPane } from '$lib/components/rb/browser/MyTagPane.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

function _storage(values) {
	return {
		getItem: (key) => values.get(key) ?? null,
		setItem: (key, value) => values.set(key, value),
		removeItem: (key) => values.delete(key)
	};
}

async function _withWindow(values, body) {
	const original = globalThis.window;
	globalThis.window = { localStorage: _storage(values) };
	try {
		return await body();
	} finally {
		if (original === undefined) delete globalThis.window;
		else globalThis.window = original;
	}
}

const brief = (groups) => groups.map((g) => [g.id, g.tags.map((t) => `${t.name}:${t.state}`)]);

describe('My Tag pane state', () => {
	it('groups the catalog by tag type in type order, untyped and unknown-type tags under Other', () => {
		/** [if] a typed tag lands under Other or the type order is lost [then] the groups misfile tags, [else stop]. */
		const groups = state.myTagPaneGroups(CATALOG, TYPES, ASSIGNMENTS, ['a']);
		assert.deepEqual(brief(groups), [
			['genre', ['House:checked']],
			['mood', ['Dark:checked']],
			['other', ['Odd:clear', 'Warmup:clear']]
		]);
		assert.equal(groups[2].label, 'Other');
	});

	it('reads checked when every selected track holds a tag, mixed when some do, clear when none', () => {
		/** [if] Dark on one of two selected rows does not read mixed [then] a tick overwrites the other row, [else stop]. */
		const groups = state.myTagPaneGroups(CATALOG, TYPES, ASSIGNMENTS, ['a', 'b', 'b']);
		assert.deepEqual(brief(groups).slice(0, 2), [
			['genre', ['House:checked']],
			['mood', ['Dark:mixed']]
		]);
		assert.equal(groups[1].tags[0].held, 1);
		assert.deepEqual(brief(state.myTagPaneGroups(CATALOG, TYPES, ASSIGNMENTS, []))[0], [
			'genre',
			['House:clear']
		]);
	});

	it('a click on a checked tag takes it off every selected track; mixed or clear puts it on all of them', () => {
		/** [if] a mixed click removes or a checked click adds [then] the selection ends up half tagged, [else stop]. */
		assert.deepEqual(state.myTagToggleWrite({ name: 'House', state: 'checked' }, ['a', 'b', 'a']), {
			stable_ids: ['a', 'b'],
			add: [],
			remove: ['House']
		});
		assert.deepEqual(state.myTagToggleWrite({ name: 'Dark', state: 'mixed' }, ['a', 'b']), {
			stable_ids: ['a', 'b'],
			add: ['Dark'],
			remove: []
		});
		assert.throws(
			() => state.myTagToggleWrite({ name: 'Dark', state: 'clear' }, []),
			/at least one/
		);
	});
});

describe('My Tag pane render', () => {
	const render = (props) =>
		ssr.render(ssr.MyTagPane, { props: { busy: false, ontoggle: () => {}, ...props } }).body;

	it('shows one fieldset per group and a checkbox per tag carrying its state', () => {
		/** [if] a group, a tag or its mixed state is missing from the pane [then] the DJ cannot see the selection's tags, [else stop]. */
		const groups = state.myTagPaneGroups(CATALOG, TYPES, ASSIGNMENTS, ['a', 'b']);
		const html = render({ groups, selectedCount: 2 });
		const groupIds = [...html.matchAll(/data-testid="mytag-group" data-group="([^"]*)"/g)].map(
			(m) => m[1]
		);
		assert.deepEqual(groupIds, ['genre', 'mood', 'other']);
		const checks = [...html.matchAll(/data-tag="([^"]*)" data-state="([^"]*)"/g)].map(
			(m) => `${m[1]}:${m[2]}`
		);
		assert.deepEqual(checks, ['House:checked', 'Dark:mixed', 'Odd:clear', 'Warmup:clear']);
		assert.match(html, /<legend[^>]*>Mood<\/legend>/);
		assert.match(html, /2 tracks/);
	});

	it('shows an empty-state line, not a blank column, when nothing is selected', () => {
		/** [if] no selection renders checkboxes or nothing at all [then] the pane reads as broken, [else stop]. */
		const html = render({ groups: [], selectedCount: 0 });
		assert.match(html, /data-testid="mytag-pane-empty"[^>]*>\s*Select a track/);
		assert.doesNotMatch(html, /data-testid="mytag-check"/);
	});
});

describe('My Tag pane toggle', () => {
	it('persists through the library_mytag_pane pref across a reload', async () => {
		/** [if] the pane is turned on then the prefs module reloads [then] it is still on, [else stop]. */
		const values = new Map([[STORAGE_KEY, JSON.stringify({ hide_broken_links: false })]]);
		await _withWindow(values, async () => {
			const first = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
			assert.equal(
				first.uiPrefs.library_mytag_pane,
				false,
				'an old blob without the key loads as off'
			);
			first.setLibraryMyTagPane(true);
			assert.equal(JSON.parse(values.get(STORAGE_KEY)).library_mytag_pane, true);
			assert.throws(() => first.setLibraryMyTagPane('yes'), TypeError);
			const reloaded = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
			assert.equal(reloaded.uiPrefs.library_mytag_pane, true);
		});
	});

	it('library_mytag_pane sets, flips and reports the pane without the UI', async () => {
		/** [if] library_mytag_pane runs with enabled and without [then] the pref and library_panels.mytag_pane follow, [else stop]. */
		await _withWindow(new Map(), async () => {
			const bus = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
				viteApiBase: 'https://mytag-pane.example.test'
			});
			const uninstall = bus.installPerformanceBrowserIpc();
			try {
				assert.equal(bus.queryPerformanceState().library_panels.mytag_pane, false);
				await bus.dispatchPerformanceCommand({ type: 'library_mytag_pane', enabled: true });
				assert.equal(bus.uiPrefs.library_mytag_pane, true);
				assert.equal(bus.queryPerformanceState().library_panels.mytag_pane, true);
				await bus.dispatchPerformanceCommand({ type: 'library_mytag_pane' });
				assert.equal(bus.queryPerformanceState().library_panels.mytag_pane, false);
			} finally {
				uninstall();
			}
		});
	});

	it('refuses a non-boolean or an unknown field and never queues behind a deck', async () => {
		/** [if] enabled is "yes" or a stray field rides along [then] the parser refuses it and the scope is null, [else stop]. */
		const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
		assert.throws(
			() => ipc.parsePerformanceCommandForTest({ type: 'library_mytag_pane', enabled: 'yes' }),
			/boolean/
		);
		assert.throws(
			() => ipc.parsePerformanceCommandForTest({ type: 'library_mytag_pane', pane: 1 }),
			/unexpected fields/
		);
		assert.equal(ipc.performanceCommandQueueScopes({ type: 'library_mytag_pane' }), null);
	});
});
