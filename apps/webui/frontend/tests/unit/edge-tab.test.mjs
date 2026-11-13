// requirement: EDGESAMP-14223
/**
 * Issue #10784 (S7): a filled edge tab shows the mock's tab (variant D `.wslot`): number cap,
 * cue stripe, play, repeat, title and meta line, region waveform, pencil and pin dot, drawn
 * from the store's real slots on main's EdgeWave.
 *
 * Regression lines:
 * - if the catalogue holds 2,000 slots then node count grows with it instead of 2 x 20 tabs
 * - if a tab's waveform drops, pads or rescales part of the region then it no longer shows the cut
 * - if an awake tab has no number or its ref does not resolve to the slot it shows then refs break
 * - if the tab's elements, order, labels or tokens drift from the mock then it stops looking like it
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ssr;
let draw;
before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as EdgeWave } from '$lib/components/rb/sampler/EdgeWave.svelte';",
			"export { DEFAULT_WAVE } from '$lib/player/sampler/edge-wave';",
			"export * as ui from '$lib/player/sampler/sampler-store.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{ css: 'injected' }
	);
	draw = await loadTypeScriptModule('src/lib/components/rb/sampler/region-wave-draw.ts');
});

const slot = (i, o = {}) => ({
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
	title: `Track ${i}`,
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
	peaks: { low: [0.5, 0.2], mid: [0.4, 0.1], high: [0.3, 0.1] },
	stems_ready: false,
	stem_parts: null,
	...o
});

/** Load `catalogue` into the bundled store through a fetch that answers like the sampler routes. */
async function load(catalogue) {
	const json = (body) => new Response(JSON.stringify(body), { status: 200 });
	ssr.ui.setSamplerFetch(async (url) => {
		const u = new URL(String(url));
		if (u.pathname.endsWith('/slots')) {
			const offset = Number(u.searchParams.get('offset'));
			const slots = catalogue.slice(offset, offset + Number(u.searchParams.get('limit')));
			return json({ slots, total: catalogue.length, skipped_no_grid: 0, rekordbox: 'ok' });
		} else if (u.pathname.endsWith('/sets')) return json({ sets: [] });
		else if (u.pathname.endsWith('/pins')) return json({ pins: [] });
		return new Response('no route', { status: 404 });
	});
	await ssr.ui.loadSampler();
}

/** Markup and scoped CSS of one server-rendered edge. */
function renderEdge(edge) {
	const { head, body } = ssr.render(ssr.EdgeWave, {
		props: {
			edge,
			top: 28,
			height: 800,
			params: ssr.DEFAULT_WAVE,
			enabled: true,
			reduceMotion: true
		}
	});
	const css = [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
	return { body, css };
}

/** The markup of each filled tab, in slot order. */
const filledTabs = (body) =>
	body.split(/(?=<div[^>]*class="wslot)/).filter((t) => /data-slot-id="/.test(t));

test('a 2,000 slot catalogue renders at most 2 x 32 tab nodes', async () => {
	/** [if] the catalogue holds 2,000 slots [then] both edges render 20 tabs each, all filled, never more than 64, [else stop]. */
	await load(Array.from({ length: 2000 }, (_, i) => slot(i)));
	const bodies = ['L', 'R'].map((e) => renderEdge(e).body);
	const nodes = bodies.map((b) => b.match(/class="wslot\b/g)?.length ?? 0);
	assert.deepEqual(nodes, [20, 20]);
	assert.ok(nodes[0] + nodes[1] <= 64);
	for (const b of bodies) assert.equal(filledTabs(b).length, 20);
	assert.equal(bodies.join('').match(/data-slot-id="/g).length, 40);
});

test('the tab waveform covers exactly the region peaks, first to last', () => {
	/** [if] a region's peaks are drawn [then] the first and last peak land in the first and last column, nothing is cut, [else stop]. */
	const n = 400;
	const flat = Array(n).fill(0.1);
	const spike = (at) => flat.map((v, i) => (i === at ? 1 : v));
	for (const width of [100, 200, 400]) {
		const first = draw.regionColumnHeights({ low: spike(0), mid: flat, high: flat }, width);
		const last = draw.regionColumnHeights({ low: spike(n - 1), mid: flat, high: flat }, width);
		assert.equal(first.low.length, width);
		assert.equal(first.low.indexOf(Math.max(...first.low)), 0);
		assert.equal(last.low.lastIndexOf(Math.max(...last.low)), width - 1);
		assert.equal(first.low.filter((v) => v > 0.5).length, 1);
	}
	// Mid and high draw at the deck waves' band scales, under the low band.
	const one = draw.regionColumnHeights({ low: [1], mid: [1], high: [1] }, 4);
	assert.ok(one.mid[0] < one.low[0] && one.high[0] < one.mid[0]);
	assert.throws(() => draw.regionColumnHeights({ low: [1, 1], mid: [1], high: [1, 1] }, 4), /same/);
});

test('every awake tab shows its number and a ref that resolves to its slot', async () => {
	/** [if] 45 slots load [then] each L tab shows 01..20, its tip starts S2-<nn>, and that ref resolves to the slot, [else stop]. */
	await load(Array.from({ length: 45 }, (_, i) => slot(i)));
	const { body, css } = renderEdge('L');
	const tabs = filledTabs(body);
	assert.equal(tabs.length, 20);
	tabs.forEach((tab, i) => {
		const nn = String(i + 1).padStart(2, '0');
		assert.match(tab, new RegExp(`<span class="num[^"]*">${nn}</span>`));
		const ref = tab.match(/title="(S\d+-\d\d) /)[1];
		assert.equal(ref, `S2-${nn}`);
		assert.equal(ssr.ui.resolveRef(ref).slot_id, tab.match(/data-slot-id="([^"]+)"/)[1]);
	});
	assert.match(css, /\.edge-wave[^{]*\.awake[^{]*\.num[^{]*\{[^}]*opacity: ?1/);
});

test('a filled tab carries the mock tab elements, order, labels and tokens', async () => {
	/** [if] a filled R tab renders [then] it has the mock's elements in order, its labels and meta, from --es- tokens only, [else stop]. */
	await load([
		slot(0, { title: 'Low Tide', vector: { eq: {} }, settings: { ...slot(0).settings, repeat: 4 } })
	]);
	ssr.ui.setEdgeSearch('R', 'tide');
	try {
		const { body, css } = renderEdge('R');
		const [tab] = filledTabs(body);
		const at = (re) => tab.search(re);
		const order = [
			/class="edge/,
			/class="num/,
			/class="pinmark/,
			/class="in/,
			/class="play/,
			/class="rep/,
			/class="txt/,
			/class="lb/,
			/class="ft/,
			/class="wv/,
			/<canvas/,
			/class="ph/,
			/class="pen/
		];
		const pos = order.map(at);
		assert.ok(
			pos.every((p) => p >= 0),
			`all present: ${pos}`
		);
		assert.deepEqual(
			[...pos].sort((a, b) => a - b),
			pos,
			'mock DOM order'
		);
		assert.match(tab, /aria-label="Play on the master at the next downbeat"/);
		assert.match(
			tab,
			/class="wv[^"]*"[^>]*title="Click: preview in the headphones only, beat and key matched"/
		);
		assert.match(tab, /aria-label="Sample settings"[^>]*><svg[^>]*><path d="M16\.5 3\.5/);
		assert.match(tab, /<sup[^>]*>4<\/sup>/);
		assert.match(tab, /Loop the first 4 beats\. Click for loop the first 2 beats\./);
		assert.match(tab, /Low <!--\]--><!--\[0--><mark[^>]*>Tide<\/mark>/);
		assert.match(tab, /class="cue[^"]*">A 4 bars</);
		assert.match(tab, /class="k c2[^"]*">8A</);
		assert.match(tab, /class="bpm[^"]*" title="Tempo of this sample as captured, in BPM">124\.0</);
		assert.match(tab, /class="kept[^"]*"[^>]*>MIX</);
		assert.match(tab, /title="S1-01  Low Tide \. A 4 bars"/);
		const rule = (sel, decl) =>
			assert.match(
				css,
				new RegExp(`${sel}[^{]*\\{[^}]*${decl.replace(': ', ': ?')}`),
				`${sel} { ${decl} }`
			);
		rule('\\.in', 'width: 428px');
		rule('\\.in', 'grid-template-columns: 24px minmax\\(0, 1fr\\) 22px 100px 28px');
		rule(
			'\\.edge-wave\\.L[^{]*\\.in',
			'grid-template-columns: 28px 100px 22px minmax\\(0, 1fr\\) 24px'
		);
		rule('\\.edge-wave\\.R[^{]*\\.play', 'order: 5');
		rule('\\.edge-wave\\.L[^{]*\\.play', 'order: 1');
		rule('\\.play', 'background: var\\(--es-play-bg\\)');
		rule('\\.rep', 'color: var\\(--es-dim\\)');
		rule('\\.rep\\.looping', 'color: var\\(--es-muted\\)');
		rule('\\.ft', 'font-family: var\\(--es-font-mono\\)');
		rule('\\.kept', 'border: 1px solid var\\(--es-mix-border\\)');
		rule('\\.wv', 'height: 34px');
		rule('\\.pen', 'opacity: var\\(--treveal, 0\\)');
		rule('\\.wslot\\.pinned[^{]*\\.pinmark', 'display: block');
		const tabCss = css.slice(css.indexOf('.pinmark'));
		assert.deepEqual(tabCss.match(/#[0-9a-f]{3,8}\b|rgba?\(/gi) ?? [], []);
		assert.deepEqual(tabCss.match(/var\(--rb-[\w-]+\)/g) ?? [], []);
	} finally {
		ssr.ui.setEdgeSearch('R', '');
	}
});
