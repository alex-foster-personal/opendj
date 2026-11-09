// LIBUX-10046: Related Tracks criteria prefs, the Related tab's fetch
// controller, the criteria cog popover and the PaneTabs Related tab.
//
// Regression lines:
// - if the defaults stop being the #9753 set then a fresh cog changes Related
// - if relatedCriteriaQuery omits a param then stored prefs silently override the UI
// - if a stale fetch overwrites a newer one then the list shows the wrong seed
// - if the popover imports prefs.svelte then it fans in the whole prefs graph
// - if browser_related is missing from the bus then agents cannot switch to the Related tab
// - if the tab click bypasses browser_related then the UI and agent paths diverge
// - if the cog writes prefs directly then agents cannot edit criteria over IPC (PRRT_kwDOSEvNd861J-iH)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';
import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '../..');
const read = (rel) => readFileSync(join(FRONTEND_ROOT, rel), 'utf8');
const POPOVER = read('src/lib/components/rb/browser/RelatedCriteriaPopover.svelte');
const BROWSER_PANEL = read('src/lib/components/rb/BrowserPanel.svelte');

const DEFAULTS = {
	bpm_percent: 6,
	key: true,
	genre: true,
	color: false,
	rating: false,
	mytag: false,
	artist: false
};

let prefs;
let ssr;
let ipc;

before(async () => {
	prefs = await loadTypeScriptModule('src/lib/rb/related-criteria-prefs.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as Popover } from '$lib/components/rb/browser/RelatedCriteriaPopover.svelte';",
			"export { default as PaneTabs } from '$lib/components/rb/browser/PaneTabs.svelte';",
			"export { default as RelatedList } from '$lib/components/rb/browser/RelatedTracksList.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

test('defaults are the #9753 set and the query sends all seven params', () => {
	assert.deepEqual({ ...prefs.RELATED_CRITERIA_DEFAULTS }, DEFAULTS);
	const q = prefs.relatedCriteriaQuery({ ...DEFAULTS, bpm_percent: 4, color: true });
	assert.equal(q.toString(), 'bpm_percent=4&key=1&genre=1&color=1&rating=0&mytag=0&artist=0');
});

test('validator keeps valid fields and drops an out-of-range bpm_percent', () => {
	for (const bad of [0, 21, 4.5]) {
		assert.deepEqual(
			prefs.validateRelatedCriteriaPrefs({ bpm_percent: bad, color: true }, 'test'),
			{
				color: true
			}
		);
	}
	assert.deepEqual(prefs.validateRelatedCriteriaPrefs({ bpm_percent: 20 }, 'test'), {
		bpm_percent: 20
	});
	assert.throws(() => prefs.validateRelatedCriteriaPrefs('nope', 'test'));
});

test('controller: null seed is idle, a stale response never overwrites, errors become state', async () => {
	const mod = await loadRuneModule(
		"export { createRelatedTracks } from '$lib/components/rb/browser/related-tracks.svelte';"
	);
	const pending = [];
	const seen = [];
	const related = mod.createRelatedTracks({
		fetch: (sid, criteria) => {
			seen.push([sid, criteria.bpm_percent]);
			return new Promise((resolve, reject) => pending.push({ sid, resolve, reject }));
		}
	});
	await related.refresh(null, '', DEFAULTS);
	assert.equal(related.state.kind, 'idle');

	const first = related.refresh('a', 'A', DEFAULTS);
	const second = related.refresh('b', 'B', { ...DEFAULTS, bpm_percent: 3 });
	assert.deepEqual(seen, [
		['a', 6],
		['b', 3]
	]);
	pending[1].resolve({ items: [{ stable_id: 'x' }] });
	await second;
	pending[0].resolve({ items: [{ stable_id: 'stale' }] });
	await first;
	assert.equal(related.state.kind, 'loaded');
	assert.equal(related.state.seedTitle, 'B');
	assert.deepEqual(
		related.state.items.map((r) => r.stable_id),
		['x']
	);

	const failed = related.refresh('c', 'C', DEFAULTS);
	pending[2].reject(new Error('boom'));
	assert.match(await failed, /boom/);
	assert.equal(related.state.kind, 'error');
});

test('popover renders a closed cog trigger and never imports prefs.svelte', () => {
	const html = ssr.render(ssr.Popover, {
		props: { criteria: DEFAULTS, onchange: () => {}, onreset: () => {} }
	}).body;
	assert.match(html, /data-testid="related-criteria-cog"/);
	assert.match(html, /aria-haspopup="dialog"/);
	assert.match(html, /aria-expanded="false"/);
	assert.doesNotMatch(POPOVER, /from '\$lib\/rb\/prefs\.svelte'/);
	for (const id of ['related-bpm-percent', 'related-criteria-reset', 'related-crit-${flag.name}']) {
		assert.ok(POPOVER.includes(id), id);
	}
	assert.match(POPOVER, /event\.key !== 'Escape'/);
	assert.match(POPOVER, /onpointerdown=\{onWindowPointerDown\}/);
	assert.match(POPOVER, /onclick=\{\(\) => onreset\(\)\}/);
});

test('PaneTabs shows the Related tab only with onrelated', () => {
	const base = {
		tabs: [{ title: 'Collection', count: 3, loading: false, sticky: false, ephemeral: false }],
		active: 0,
		onactivate: () => {},
		ontogglesticky: () => {},
		onreorder: () => {},
		ondropplaylist: () => {},
		onsaveas: () => {}
	};
	assert.doesNotMatch(ssr.render(ssr.PaneTabs, { props: base }).body, /pane-tab-related/);
	const inactive = ssr.render(ssr.PaneTabs, { props: { ...base, onrelated: () => {} } }).body;
	assert.match(inactive, /pane-tab-related/);
	assert.match(inactive, /aria-selected="true"/);
	const active = ssr.render(ssr.PaneTabs, {
		props: { ...base, onrelated: () => {}, relatedActive: true }
	}).body;
	assert.match(active, /class="tab active related-tab[^"]*"/);
	assert.doesNotMatch(active, /lock this pane tab/);
});

test('Related list states are explicit, never an empty list', () => {
	const render = (state) =>
		ssr.render(ssr.RelatedList, { props: { state, onload: () => {}, onplay: () => {} } }).body;
	assert.match(render({ kind: 'idle' }), /Select a track/);
	assert.match(render({ kind: 'error', message: 'related tracks failed: x' }), /related-error/);
	assert.match(
		render({ kind: 'loaded', seedTitle: 'Tune', items: [] }),
		/No related tracks for Tune/
	);
	const html = render({
		kind: 'loaded',
		seedTitle: 'Tune',
		items: [
			{
				stable_id: 's',
				title: 'Hit',
				artist: 'A',
				bpm: 120,
				key: '8A',
				genre: 'house',
				score: 2,
				matched: ['bpm', 'key']
			}
		]
	});
	assert.match(html, /Hit/);
	assert.match(html, /bpm, key/);
});

test('BrowserPanel wires the cog through typed IPC and refetches on criteria', () => {
	assert.match(
		BROWSER_PANEL,
		/runPerformanceCommandFromUi\(\{ type: 'browser_related_criteria', \.\.\.patch \}\)/
	);
	assert.match(
		BROWSER_PANEL,
		/runPerformanceCommandFromUi\(\{ type: 'browser_related_criteria_reset' \}\)/
	);
	assert.doesNotMatch(BROWSER_PANEL, /patchRelatedCriteria|resetRelatedCriteria/);
	assert.match(BROWSER_PANEL, /\{ \.\.\.uiPrefs\.library_related_criteria \}/);
	assert.match(BROWSER_PANEL, /\{:else if relatedActive\}/);
});

test('browser_related_criteria parses strictly', () => {
	assert.deepEqual(
		ipc.parsePerformanceCommandForTest({
			type: 'browser_related_criteria',
			bpm_percent: 4,
			artist: true
		}),
		{ type: 'browser_related_criteria', bpm_percent: 4, artist: true }
	);
	assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'browser_related_criteria_reset' }), {
		type: 'browser_related_criteria_reset'
	});
	for (const bad of [
		{ type: 'browser_related_criteria' },
		{ type: 'browser_related_criteria', bpm_percent: 0 },
		{ type: 'browser_related_criteria', bpm_percent: 21 },
		{ type: 'browser_related_criteria', bpm_percent: 4.5 },
		{ type: 'browser_related_criteria', bpm_percent: '4' },
		{ type: 'browser_related_criteria', key: 1 },
		{ type: 'browser_related_criteria', artist: true, extra: 1 },
		{ type: 'browser_related_criteria_reset', extra: 1 }
	]) {
		assert.throws(() => ipc.parsePerformanceCommandForTest(bad));
	}
});

test('browser_related parses strictly', () => {
	assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'browser_related', active: true }), {
		type: 'browser_related',
		active: true
	});
	for (const bad of [
		{ type: 'browser_related', active: 'yes' },
		{ type: 'browser_related' },
		{ type: 'browser_related', active: true, extra: 1 }
	]) {
		assert.throws(() => ipc.parsePerformanceCommandForTest(bad));
	}
});

test('browser_related fails loudly with no browser and reads back related_active', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		assert.equal(ipc.queryPerformanceState().browser.related_active, false);
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'browser_related', active: true }),
			/requires a mounted browser panel/
		);
		let active = false;
		const unregister = ipc.registerPerformanceBrowserAdapter({
			selectPlaylist: async () => {},
			setRelatedActive: (value) => {
				active = value;
			},
			readSnapshot: () => ({
				search: null,
				sort: null,
				selected_row: null,
				load: null,
				related_active: active
			})
		});
		try {
			await ipc.dispatchPerformanceCommand({ type: 'browser_related', active: true });
			assert.equal(ipc.queryPerformanceState().browser.related_active, true);
			await ipc.dispatchPerformanceCommand({ type: 'browser_related', active: false });
			assert.equal(ipc.queryPerformanceState().browser.related_active, false);
		} finally {
			unregister();
		}
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('the Related tab click goes through browser_related and one writer sets relatedActive', () => {
	assert.match(
		BROWSER_PANEL,
		/onrelated=\{\(\) =>\s+void runPerformanceCommandFromUi\(\{ type: 'browser_related', active: true \}\)\}/
	);
	assert.doesNotMatch(BROWSER_PANEL, /relatedActive = true/);
	assert.equal(BROWSER_PANEL.match(/\brelatedActive = (?!\$state)/g).length, 1);
});
