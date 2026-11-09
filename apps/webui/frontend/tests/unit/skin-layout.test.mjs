// requirement: SKINLAYOUT-8869, SKINLAYOUT-10489
// [if] a skin other than Green Room selects the three-panel library layout [then] Gothic, Light or Default change shape, [else stop].
// [if] the Now Playing panel prefers the preview over the last Gig master deck [then] it shows the wrong track, [else stop].
// [if] the Your Library search does work with an empty query [then] the library listing path gains cost, [else stop].
// [if] a skin's performance preset is not deck-grid, or the deck-grid spec's areas drift from the /performance grid [then] the performance root changes shape with no declared preset, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let L;
let skin;
before(async () => {
	L = await loadTypeScriptModule('src/lib/rb/skin-layout.ts');
	skin = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
});

test('if any skin lacks a library layout, or a skin other than Green Room bends, then the manifest is wrong', () => {
	for (const s of skin.UI_SKIN_CHOICES)
		assert.ok(L.LIBRARY_LAYOUTS.includes(L.libraryLayoutFor(s)), s);
	assert.equal(L.libraryLayoutFor('green-room'), 'three-panel');
	for (const s of ['default', 'mono-dev', 'light'])
		assert.equal(L.libraryLayoutFor(s), 'crate-desk');
	assert.throws(() => L.parseLibraryLayout('sidebar-left'), /library layout must be/);
	assert.equal(L.parseLibraryLayout('three-panel'), 'three-panel');
});

test('if a skin lacks the deck-grid performance preset, or its spec drifts from the /performance grid, then the manifest is wrong', () => {
	assert.deepEqual([...L.PERFORMANCE_LAYOUTS], ['deck-grid']);
	for (const s of skin.UI_SKIN_CHOICES) assert.equal(L.performanceLayoutFor(s), 'deck-grid', s);
	assert.deepEqual(
		Object.keys(L.PERFORMANCE_LAYOUT_SPEC).sort(),
		[...L.PERFORMANCE_LAYOUTS].sort()
	);
	assert.throws(
		() => L.parsePerformanceLayout('four-across'),
		/performance layout must be deck-grid/
	);
	assert.equal(L.parsePerformanceLayout('deck-grid'), 'deck-grid');

	const spec = L.PERFORMANCE_LAYOUT_SPEC['deck-grid'];
	assert.deepEqual(spec.honours, { deckLayout: true, perfView: true, pads: true });
	assert.deepEqual(spec.floors, { wavestackRows: 4, deckRowPx: 497, libraryPx: 272 });

	const page = readFileSync('src/routes/performance/+page.svelte', 'utf8');
	const root = page.indexOf('.perf-root {');
	assert.ok(root > 0, '.perf-root block missing');
	const at = page.indexOf('grid-template-areas:', root);
	const decl = page.slice(at, page.indexOf(';', at));
	const rows = [...decl.matchAll(/'([^']*)'/g)].map((m) => m[1]);
	assert.deepEqual(rows, spec.areas);
	assert.match(page, /class="perf-root"\s+data-layout=\{perfLayout\}/);
});

test('if the now-playing pick ignores the stored master deck, or falls through wrongly, then the panel lies', () => {
	const session = { master_deck: 2, decks: { 1: { stable_id: 'a' }, 2: { stable_id: 'b' } } };
	assert.deepEqual(L.nowPlayingId({ session, previewId: 'p', selectedId: 's' }), {
		stable_id: 'b',
		source: 'master-deck'
	});
	const emptyMaster = { master_deck: 1, decks: { 1: { stable_id: null } } };
	assert.deepEqual(L.nowPlayingId({ session: emptyMaster, previewId: 'p', selectedId: 's' }), {
		stable_id: 'p',
		source: 'preview'
	});
	assert.deepEqual(L.nowPlayingId({ session: null, previewId: null, selectedId: 's' }), {
		stable_id: 's',
		source: 'selection'
	});
	assert.equal(L.nowPlayingId({ session: null, previewId: null, selectedId: null }), null);
});

test('if next in queue repeats the playing track, or About shows empty fields, then the cards mislead', () => {
	assert.equal(L.nextInQueueId(['b', 'c'], 'b'), 'c');
	assert.equal(L.nextInQueueId(['c'], 'b'), 'c');
	assert.equal(L.nextInQueueId([], 'b'), null);
	const facts = L.aboutFacts({
		artist: 'X',
		album: null,
		bpm: 124,
		key: '',
		play_count: 3,
		tags: []
	});
	assert.deepEqual(
		facts.map((f) => f.label),
		['Artist', 'BPM', 'Plays']
	);
	assert.equal(facts[1].value, '124.0');
});

test('if the device strip guesses a name, or progress escapes 0..1, then the player strip lies', () => {
	assert.equal(L.playingOnLabel('MacBook Speakers'), 'Playing on MacBook Speakers');
	assert.equal(L.playingOnLabel(null), 'Playing on the system default output');
	assert.equal(L.playingOnLabel('  '), 'Playing on the system default output');
	assert.equal(L.progressRatio(5, 10), 0.5);
	assert.equal(L.progressRatio(20, 10), 1);
	assert.equal(L.progressRatio(5, 0), 0);
});

test('if the tree search copies with an empty query, or drops a match folder path, then it costs or hides', () => {
	const tree = [
		{
			name: 'Gigs',
			children: [
				{ name: 'Loft', children: [] },
				{ name: 'Warmup', children: [] }
			]
		},
		{ name: 'loft edits', children: [] }
	];
	assert.equal(L.filterTreeByName(tree, '  '), tree, 'blank query must return the same array');
	const hit = L.filterTreeByName(tree, 'LOFT');
	assert.deepEqual(
		hit.map((n) => n.name),
		['Gigs', 'loft edits']
	);
	assert.deepEqual(
		hit[0].children.map((n) => n.name),
		['Loft']
	);
	assert.deepEqual(L.filterTreeByName(tree, 'nope'), []);
});

test('if a three-panel text or indicator pair falls under WCAG AA on the Green Room tokens then the panels are illegible', async () => {
	const cc = await loadTypeScriptModule('src/lib/rb/color-contrast.ts');
	const theme = readFileSync('src/lib/rb/theme.css', 'utf8');
	const start = theme.indexOf("html[data-skin='green-room'] .perf-root {");
	const body = theme.slice(start, theme.indexOf('}', start));
	const tok = (name) => {
		const m = body.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`));
		assert.ok(m, `${name} missing from the Green Room block`);
		return m[1];
	};
	const [panel, bg, text, dim] = ['--rb-panel', '--rb-bg', '--rb-text', '--rb-text-dim'].map(tok);
	for (const surface of [panel, bg]) {
		assert.ok(cc.contrastRatio(text, surface) >= 4.5, `text on ${surface}`);
		assert.ok(cc.contrastRatio(dim, surface) >= 4.5, `dim text on ${surface}`);
	}
	// PlayingOnStrip literals: black ink on the green strip, white progress on its track (non-text, 3:1).
	assert.ok(cc.contrastRatio('#000000', '#1ed760') >= 4.5);
	assert.ok(cc.contrastRatio('#ffffff', '#4d4d4d') >= 3);
});
