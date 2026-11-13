// requirement: EDGESAMP-14302
/**
 * Issue #10784 (S7, S7b): the top tab shows the mock's head (variant D `.whead`): set name,
 * `set n / N`, paging arrows, search, the library's filter icons, compatible and its caret,
 * and the counts line; an edge pages by arrows, wheel or keys, popping top to bottom.
 *
 * Regression lines:
 * - if a named set's name or description is missing from its top tab then naming sets is pointless
 * - if the wheel flips more than one set per 260 ms then a trackpad swipe skips sets
 * - if a flip pops all tabs at once or moves the other edge then paging loses its place
 * - if the head's elements, order, labels or tokens drift from the mock then it stops looking like it
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let ssr;
before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as EdgeWave } from '$lib/components/rb/sampler/EdgeWave.svelte';",
			"export * as wave from '$lib/player/sampler/edge-wave';",
			"export * as ui from '$lib/player/sampler/sampler-store.svelte';",
			"export { LIBRARY_FILTER_LABELS } from '$lib/rb/library-filter-labels';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{ css: 'injected' }
	);
});

const slot = (i) => ({
	slot_id: `s${i}`,
	stable_id: `t${i}`,
	source: 'loop_hot_cue',
	cue_letter: 'A',
	in_ms: 0,
	out_ms: 8000,
	beats: 16,
	native_bpm: 124,
	key: '8A',
	energy: null,
	color: '#3fa7ff',
	title: i === 3 ? 'Low Tide' : `Track ${i}`,
	artist: 'Artist',
	name: null,
	description: '',
	vector: null,
	settings: {
		launch: 'bar',
		key: 'auto',
		tempo: 'match',
		mode: 'loop',
		repeat: 'loop',
		downbeat_offset: 0,
		level: 80
	},
	flags: { broken: false, remix: false, vocals: false, offline: false },
	peaks: { low: [0.5], mid: [0.4], high: [0.3] },
	stems_ready: false,
	stem_parts: null
});

/** Load 60 slots plus `metas` into the bundled store through a fetch shaped like the routes. */
async function load(metas = []) {
	const json = (body) => new Response(JSON.stringify(body), { status: 200 });
	const catalogue = Array.from({ length: 60 }, (_, i) => slot(i));
	ssr.ui.setSamplerFetch(async (url) => {
		const u = new URL(String(url));
		if (u.pathname.endsWith('/slots'))
			return json({ slots: catalogue, total: 60, skipped_no_grid: 0, rekordbox: 'ok' });
		return json(u.pathname.endsWith('/sets') ? { sets: metas } : { pins: [] });
	});
	await ssr.ui.loadSampler();
	for (const [edge, set] of [
		['R', 0],
		['L', 1]
	])
		ssr.ui.setEdgeSet(edge, set);
}

/** The top tab markup (head only) and all scoped CSS of one rendered edge. */
function renderHead(edge) {
	const { head, body } = ssr.render(ssr.EdgeWave, {
		props: {
			edge,
			top: 28,
			height: 800,
			params: ssr.wave.DEFAULT_WAVE,
			enabled: true,
			reduceMotion: true
		}
	});
	const css = [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
	const start = body.indexOf('<div class="whead');
	return { top: body.slice(start, body.indexOf('<div class="wslot', start)), css };
}

test('a named set shows its name and description on the top tab', async () => {
	/** [if] set-1 is saved as Warmup with a description [then] the R top tab shows both and the right-click tip, [else stop]. */
	await load([{ id: 'set-1', name: 'Warmup', description: 'Rolling drums', slot_ids: null }]);
	const { top } = renderHead('R');
	assert.match(top, /class="setname[^"]*">Warmup</);
	assert.match(top, /class="pg[^"]*">set 1 \/ 3</);
	assert.match(top, /title="Rolling drums\nRight-click to name this set"/);
	assert.match(top, /class="counts[^"]*"><!--\[\d--><span[^>]*>Rolling drums</);
	const other = renderHead('L').top;
	assert.match(other, /class="setname[^"]*">Set 2</);
	assert.match(other, /<span[^>]*>Right-click the set name to name it</);
});

test('the wheel flips at most one set per 260 ms', () => {
	/** [if] wheel ticks land at 0, 100, 200, 260, 300 and 520 ms [then] only 0, 260 and 520 flip, asleep never, [else stop]. */
	assert.equal(ssr.wave.WHEEL_FLIP_MS, 260);
	let last = -1e9;
	const flips = [];
	for (const t of [0, 100, 200, 260, 300, 520]) {
		const d = ssr.wave.wheelFlip(true, 40, t, last);
		if (d !== 0) {
			last = t;
			flips.push(t);
		}
	}
	assert.deepEqual(flips, [0, 260, 520]);
	assert.equal(ssr.wave.wheelFlip(true, -3, 1000, 0), -1);
	assert.equal(ssr.wave.wheelFlip(false, 40, 1000, 0), 0);
});

test('a flip pops the new set top to bottom 18 ms apart and leaves the other edge', async () => {
	/** [if] the L edge flips [then] R keeps its set and flip stamp, and L tab k starts popping 18*k ms after the flip, [else stop]. */
	await load();
	const { sampler, flipSet } = ssr.ui;
	const rBefore = { set: sampler.edges.R.set, flipAt: sampler.edges.R.flipAt };
	flipSet('L', 1);
	assert.equal(sampler.edges.L.set, 2);
	assert.deepEqual({ set: sampler.edges.R.set, flipAt: sampler.edges.R.flipAt }, rBefore);
	const { wave } = ssr;
	const flipT = sampler.edges.L.flipAt;
	assert.ok(wave.FLIP_STAGGER_MS >= 18 && wave.FLIP_STAGGER_MS <= 22);
	const widthAt = (k, t) => {
		const m = wave.createEdgeMotion(20);
		m.awake = true;
		m.flipT = flipT;
		wave.stepEdgeMotion(m, {
			enabled: true,
			awake: true,
			armed: true,
			fromEdgePx: 8,
			pointerY: 28 + 132 + 4 + 3 * 32 + 16,
			top: 28,
			headH: 132,
			tabH: 32,
			tabCount: 20,
			buttons: 0,
			inWindow: true,
			t,
			appDrag: false,
			held: Array(20).fill(false),
			filled: Array(20).fill(true),
			params: wave.DEFAULT_WAVE,
			reduceMotion: false,
			dtMs: 1000,
			headW: m.headW,
			widths: m.widths
		});
		return m.widths[k];
	};
	for (const k of [2, 3, 4]) {
		assert.equal(widthAt(k, flipT + k * wave.FLIP_STAGGER_MS - 1), wave.AWAKE_W, `tab ${k} waits`);
		assert.ok(widthAt(k, flipT + k * wave.FLIP_STAGGER_MS + 10) > wave.AWAKE_W, `tab ${k} pops`);
	}
});

test('the top tab carries the mock head elements, order, labels and tokens', async () => {
	/** [if] the R top tab renders while searching [then] it has the mock's elements in order, labels, counts, --es- tokens, [else stop]. */
	await load();
	ssr.ui.setEdgeSearch('R', 'tide');
	try {
		const { top, css } = renderHead('R');
		const order = [
			/class="tick/,
			/class="setname/,
			/class="pg/,
			/aria-label="Previous set"/,
			/aria-label="Next set"/,
			/type="search"/,
			/class="hrow filt/,
			/class="compat/,
			/aria-label="Compatible settings"/,
			/class="counts/
		];
		const pos = order.map((re) => top.search(re));
		assert.ok(
			pos.every((p) => p >= 0),
			`all present: ${pos}`
		);
		assert.deepEqual(
			[...pos].sort((a, b) => a - b),
			pos,
			'mock DOM order'
		);
		assert.match(top, /placeholder="Search samples: title, key, bars"/);
		const L = ssr.LIBRARY_FILTER_LABELS;
		const icons = [...top.matchAll(/class="ico[^"]*"[^>]*aria-label="([^"]+)"/g)].map((m) => m[1]);
		assert.deepEqual(
			icons,
			['broken', 'remixes', 'vocals', 'offline'].map((f) => L[f].tip)
		);
		assert.match(top, /<input type="checkbox"[^>]*\/?>\s*compatible/);
		assert.match(top, /1 here \. 1 in all sets/);
		const rule = (sel, decl) =>
			assert.match(css, new RegExp(`${sel}[^{]*\\{[^}]*${decl}`), `${sel} { ${decl} }`);
		rule('\\.setname', 'font: ?18px var\\(--es-font-display\\)');
		rule('\\.pg', 'font: ?12px var\\(--es-font-mono\\)');
		rule('\\.arrow', 'background: ?var\\(--es-raised\\)');
		rule("input\\[type='search'\\]", 'background: ?var\\(--es-search-bg\\)');
		rule('\\.ico', 'width: ?30px');
		rule("\\.ico\\[aria-pressed='true'\\]", 'color: ?var\\(--es-accent-ink\\)');
		rule('\\.compat[^{]* input', 'accent-color: ?var\\(--es-accent\\)');
		rule('\\.counts[^{]* button', 'color: ?var\\(--es-deck-a\\)');
		const headCss = css.slice(css.indexOf('.whead.svelte'));
		assert.deepEqual(headCss.match(/#[0-9a-f]{3,8}\b|rgba?\(/gi) ?? [], []);
		assert.deepEqual(headCss.match(/var\(--rb-[\w-]+\)/g) ?? [], []);
	} finally {
		ssr.ui.setEdgeSearch('R', '');
	}
});
