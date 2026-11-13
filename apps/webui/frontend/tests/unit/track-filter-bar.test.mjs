import assert from 'node:assert/strict';
import { after, before, describe, it } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * The Track Filter bar (issue #10043) (rekordbox Track Filter bar, mimicked).
 *
 * [if] an active funnel has no chip, or a chip's summary is wrong [then] the bar hides a criterion - broken.
 * [if] the off switch forgets a criterion, or Clear all leaves one [then] suspend or clear lies - broken.
 * [if] a chip's selector misses its column's funnel trigger [then] clicking a chip opens nothing - broken.
 * [if] library_filter set takes an unknown field or BPM min above max [then] agents can store nonsense - broken.
 * [if] the bar's row count ignores the shown rows [then] the footer count is stale - broken.
 */

let state;
let filters;
let store;
let ipc;
let ssr;

const PANE = 'pane-a';

before(async () => {
	state = await loadTypeScriptModule('src/lib/components/rb/browser/track-filter-state.ts');
	filters = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-column-filters.ts'
	);
	store = await loadTypeScriptModule(
		'src/lib/components/rb/browser/track-table-funnel-store.svelte.ts'
	);
	ipc = await loadTypeScriptModule('tests/unit/library-filter-ipc-entry.mjs');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as TrackFilterBar } from '$lib/components/rb/browser/TrackFilterBar.svelte';",
			"export { default as ColourColumnFunnelPopover } from '$lib/components/rb/browser/ColourColumnFunnelPopover.svelte';",
			"export { default as BpmColumnFunnelPopover } from '$lib/components/rb/browser/BpmColumnFunnelPopover.svelte';",
			"export { default as KeyColumnFunnelPopover } from '$lib/components/rb/browser/KeyColumnFunnelPopover.svelte';",
			"export { default as GenreColumnFunnelPopover } from '$lib/components/rb/browser/GenreColumnFunnelPopover.svelte';",
			"export { default as RatingColumnFunnelPopover } from '$lib/components/rb/browser/RatingColumnFunnelPopover.svelte';",
			"export { default as MyTagColumnFunnelPopover } from '$lib/components/rb/browser/MyTagColumnFunnelPopover.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const FULL = () => ({
	...state.EMPTY_TRACK_FILTER_STATE,
	bpm: { min: 120, max: 128 },
	key: { selected: ['8A', '9A'] },
	rating: { minStars: 4 },
	mytag: { selected: ['warmup', 'vocal'], mode: 'and' }
});

const ROWS = [
	{ stable_id: 't1', bpm: 124, key: '8A', rating: 5 },
	{ stable_id: 't2', bpm: 100, key: '8A', rating: 5 },
	{ stable_id: 't3', bpm: 126, key: '1B', rating: 1 }
];

describe('Track Filter bar state', () => {
	it('renders one chip per non-empty criterion with its summary', () => {
		/** [if] BPM, Key, Rating and My Tag are set [then] four chips read as the issue's summaries, [else stop]. */
		assert.deepEqual(state.trackFilterChips(FULL()), [
			{ column: 'bpm', label: '120 to 128 BPM' },
			{ column: 'key', label: '8A, 9A' },
			{ column: 'rating', label: '4 stars and up' },
			{ column: 'mytag', label: 'warmup and vocal' }
		]);
		assert.deepEqual(state.trackFilterChips(state.EMPTY_TRACK_FILTER_STATE), []);
	});

	it('every funnel writes into one per-pane state the bar reads', () => {
		/** [if] the BPM and Key funnels patch pane A [then] pane A's state holds both and pane B stays empty, [else stop]. */
		const s = new store.TrackTableFunnelStore();
		s.patchBpm(PANE, { min: 120, max: 128 });
		s.patchKey(PANE, { selected: ['8A'] });
		const read = s.readState(PANE);
		assert.deepEqual(read.bpm, { min: 120, max: 128 });
		assert.deepEqual(read.key, { selected: ['8A'] });
		assert.equal(read.suspended, false);
		assert.deepEqual(state.trackFilterChips(s.readState('pane-b')), []);
	});

	it('the off switch returns every row and re-applies the criteria unchanged', () => {
		/** [if] the pane is suspended then resumed [then] rows return, and the same criteria filter again, [else stop]. */
		const s = new store.TrackTableFunnelStore();
		s.replaceState(PANE, FULL());
		const apply = () =>
			filters.applyTrackTableColumnFilters(ROWS, s.effective(PANE), () => ['warmup', 'vocal']);
		assert.deepEqual(
			apply().map((r) => r.stable_id),
			['t1']
		);
		s.setSuspended(PANE, true);
		assert.equal(apply().length, 3);
		assert.deepEqual(
			state.trackFilterChips(s.readState(PANE)).length,
			4,
			'suspended criteria are kept'
		);
		s.setSuspended(PANE, false);
		assert.deepEqual(s.readState(PANE), FULL());
		assert.deepEqual(
			apply().map((r) => r.stable_id),
			['t1']
		);
	});

	it('Clear all empties every criterion and switches the filter back on', () => {
		/** [if] Clear all runs on a suspended full pane [then] the state equals the empty state, [else stop]. */
		const s = new store.TrackTableFunnelStore();
		s.replaceState(PANE, { ...FULL(), suspended: true });
		s.clear(PANE);
		assert.deepEqual(s.readState(PANE), state.EMPTY_TRACK_FILTER_STATE);
	});

	it('refuses an unknown field, a bad value and a BPM min above max, with a reason', () => {
		/** [if] set gets an unknown field or min 130 above max 120 [then] it throws naming the field, [else stop]. */
		assert.throws(() => state.parseTrackFilterState({ tempo: {} }), /unknown field state\.tempo/);
		assert.throws(
			() => state.parseTrackFilterState({ bpm: { min: 1, step: 2 } }),
			/unknown field state\.bpm\.step/
		);
		assert.throws(
			() => state.parseTrackFilterState({ bpm: { min: 130, max: 120 } }),
			/min is above/
		);
		assert.throws(() => state.parseTrackFilterState({ rating: { minStars: 6 } }), /0 to 5/);
		assert.throws(
			() => state.parseTrackFilterState({ key: { selected: ['not a key'] } }),
			/invalid entry/
		);
		assert.deepEqual(state.parseTrackFilterState({ bpm: { min: 120 } }), {
			...state.EMPTY_TRACK_FILTER_STATE,
			bpm: { min: 120, max: null }
		});
	});
});

describe('Track Filter bar component', () => {
	const render = (props) =>
		ssr.render(ssr.TrackFilterBar, {
			props: { onsuspend() {}, onclear() {}, onopen() {}, ...props }
		}).body;

	it('shows each chip, the switch and the filtered row count of the total', () => {
		/** [if] the bar renders a full state with 1 of 3 rows shown [then] chips, Filter on and "1 of 3" render, [else stop]. */
		const html = render({ filterState: FULL(), shown: 1, total: 3 });
		for (const label of ['120 to 128 BPM', '8A, 9A', '4 stars and up', 'warmup and vocal']) {
			assert.ok(html.includes(label), `chip ${label} missing`);
		}
		assert.match(html, /Filter on/);
		assert.match(html, /1 of 3/);
		assert.match(html, /Clear all/);
		const off = render({ filterState: { ...FULL(), suspended: true }, shown: 3, total: 3 });
		assert.match(off, /Filter off/);
		assert.match(off, /3 of 3/);
	});

	it('names a funnel trigger every column funnel really renders, so a chip click opens it', () => {
		/** [if] a column's chip selector matches no button its funnel renders [then] the chip opens nothing, [else stop]. */
		const funnels = {
			colour: [ssr.ColourColumnFunnelPopover, { filter: filters.EMPTY_COLOUR_FILTER }],
			bpm: [ssr.BpmColumnFunnelPopover, { filter: filters.EMPTY_BPM_FILTER }],
			key: [ssr.KeyColumnFunnelPopover, { filter: filters.EMPTY_KEY_FILTER }],
			genre: [ssr.GenreColumnFunnelPopover, { filter: filters.EMPTY_GENRE_FILTER, rows: [] }],
			rating: [ssr.RatingColumnFunnelPopover, { filter: filters.EMPTY_RATING_FILTER }],
			mytag: [ssr.MyTagColumnFunnelPopover, { filter: filters.EMPTY_MYTAG_FILTER }]
		};
		assert.deepEqual(
			Object.keys(funnels).sort(),
			Object.keys(state.TRACK_FILTER_FUNNEL_TRIGGERS).sort()
		);
		for (const [column, [component, props]] of Object.entries(funnels)) {
			const html = ssr.render(component, { props: { onpatch() {}, ...props } }).body;
			const selector = state.TRACK_FILTER_FUNNEL_TRIGGERS[column];
			const cls = /^\.([\w-]+)$/.exec(selector);
			const aria = /^button\[aria-label="([^"]+)"\]$/.exec(selector);
			const needle = cls
				? new RegExp(`<button[^>]*class="[^"]*\\b${cls[1]}\\b`)
				: aria
					? new RegExp(`<button[^>]*aria-label="${aria[1]}"`)
					: null;
			assert.ok(needle, `selector ${selector} has an unexpected shape`);
			assert.match(html, needle, `${column} funnel renders no ${selector}`);
		}
	});
});

describe('library_filter performance command', () => {
	let uninstall;
	before(() => {
		globalThis.window = {};
		uninstall = ipc.installPerformanceBrowserIpc();
	});
	after(() => {
		uninstall();
		delete globalThis.window;
	});

	it('get returns the state as JSON and set replaces it', async () => {
		/** [if] set stores a BPM range [then] browser.filter reads it back as plain JSON and the table applies it, [else stop]. */
		ipc.trackTableFunnelStore.activePane = PANE;
		const next = {
			...state.EMPTY_TRACK_FILTER_STATE,
			bpm: { min: 120, max: 128 },
			suspended: true
		};
		await ipc.dispatchPerformanceCommand({ type: 'library_filter', op: 'set', state: next });
		const got = await ipc.dispatchPerformanceCommand({ type: 'library_filter', op: 'get' });
		assert.deepEqual(JSON.parse(JSON.stringify(got.browser.filter)), next);
		assert.deepEqual(ipc.trackTableFunnelStore.readState(PANE), next);
	});

	it('refuses an unknown field and a BPM min above max with a reason, and needs a mounted table', async () => {
		/** [if] set carries an unknown field or min 130 above 120, or no table is mounted [then] it rejects with why, [else stop]. */
		ipc.trackTableFunnelStore.activePane = PANE;
		const before = ipc.trackTableFunnelStore.readState(PANE);
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'library_filter', op: 'set', state: { tempo: 1 } }),
			/unknown field state\.tempo/
		);
		await assert.rejects(
			ipc.dispatchPerformanceCommand({
				type: 'library_filter',
				op: 'set',
				state: { bpm: { min: 130, max: 120 } }
			}),
			/min is above/
		);
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'library_filter', op: 'peek' }),
			/get or set/
		);
		assert.deepEqual(
			ipc.trackTableFunnelStore.readState(PANE),
			before,
			'a refused set changes nothing'
		);
		ipc.trackTableFunnelStore.activePane = null;
		assert.equal(ipc.queryPerformanceState().browser.filter, null);
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'library_filter', op: 'get' }),
			/requires a mounted track table/
		);
	});
});
