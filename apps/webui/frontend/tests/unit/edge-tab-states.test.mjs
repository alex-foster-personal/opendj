// requirement: EDGESAMP-14259
/**
 * Issue #10784 (S7): edge tabs show the mock's tab states (variant D): armed pulse, playing
 * card, previewing wave, editing outline, search non-match dim, with the widths that go
 * with them (a non-match or a tab beside the edited one settles to the awake strip).
 *
 * Regression lines:
 * - if a sounding, previewing or edited tab loses its class then it looks idle while busy
 * - if a state paints from a literal colour or an --rb- var then it drifts from the mock
 * - if a non-matching or not-edited tab keeps its popped width then search and edit lose focus
 * - if reduced motion is set and the armed tab still pulses then the motion preference is ignored
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ssr;
let wave;
before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as EdgeWave } from '$lib/components/rb/sampler/EdgeWave.svelte';",
			"export { DEFAULT_WAVE } from '$lib/player/sampler/edge-wave';",
			"export { NO_ENGINE } from '$lib/player/sampler/sampler-engine-seam';",
			"export * as ui from '$lib/player/sampler/sampler-store.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{ css: 'injected' }
	);
	wave = await loadTypeScriptModule('src/lib/player/sampler/edge-wave.ts');
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
	title: i === 4 ? 'Low Tide' : `Track ${i}`,
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

/** An engine whose voices sit in fixed phases: s0 armed, s1 playing, s2 previewing. */
const PHASES = { s0: 'armed', s1: 'playing', s2: 'previewing' };
const fixedEngine = (base) => ({ ...base, phase: (id) => PHASES[id] ?? 'free' });

/** Markup and scoped CSS of the R edge after 20 slots load into the bundled store. */
async function renderBusyEdge() {
	const json = (body) => new Response(JSON.stringify(body), { status: 200 });
	const catalogue = Array.from({ length: 20 }, (_, i) => slot(i));
	ssr.ui.setSamplerFetch(async (url) => {
		const u = new URL(String(url));
		if (u.pathname.endsWith('/slots'))
			return json({ slots: catalogue, total: 20, skipped_no_grid: 0, rekordbox: 'ok' });
		return json(u.pathname.endsWith('/sets') ? { sets: [] } : { pins: [] });
	});
	await ssr.ui.loadSampler();
	ssr.ui.registerSamplerEngine(fixedEngine(ssr.NO_ENGINE));
	ssr.ui.openSlotEditor('s3', 'R');
	ssr.ui.setEdgeSearch('R', 'tide');
	try {
		const { head, body } = ssr.render(ssr.EdgeWave, {
			props: {
				edge: 'R',
				top: 28,
				height: 800,
				params: ssr.DEFAULT_WAVE,
				enabled: true,
				reduceMotion: true
			}
		});
		const css = [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
		return { body, css };
	} finally {
		ssr.ui.setEdgeSearch('R', '');
		ssr.ui.openSlotEditor(null);
		ssr.ui.registerSamplerEngine(ssr.NO_ENGINE);
	}
}

/** The class list of the tab showing `id`. */
const classOf = (body, id) =>
	body.match(new RegExp(`<div class="(wslot[^"]*)"[^>]*data-slot-id="${id}"`))[1].split(/\s+/);

test('armed, playing, previewing and edited tabs carry their state class', async () => {
	/** [if] s0 is armed, s1 playing, s2 previewing and s3 edited [then] each tab has that class and stays held, [else stop]. */
	const { body } = await renderBusyEdge();
	const want = { s0: 'armed', s1: 'playing', s2: 'previewing', s3: 'editing' };
	for (const [id, cls] of Object.entries(want)) {
		assert.ok(classOf(body, id).includes(cls), `${id} ${cls}`);
		assert.ok(classOf(body, id).includes('held'), `${id} held`);
	}
	assert.ok(!classOf(body, 's5').includes('held'));
});

test('a search dims every tab it does not match and leaves the match lit', async () => {
	/** [if] the R search is "tide" [then] only Low Tide (s4) lacks the dim class, and dim drops the row to .3, [else stop]. */
	const { body, css } = await renderBusyEdge();
	assert.ok(!classOf(body, 's4').includes('dim'));
	for (const id of ['s0', 's5', 's19']) assert.ok(classOf(body, id).includes('dim'), id);
	assert.match(css, /\.wslot\.dim[^{]*\.in[^{]*\{[^}]*opacity: ?0?\.3/);
});

test('the state looks paint from the mock tokens', async () => {
	/** [if] the edge renders [then] armed, playing, previewing and editing rules read the mock tokens, no literal or --rb-, [else stop]. */
	const { css } = await renderBusyEdge();
	const rule = (sel, decl) =>
		assert.match(css, new RegExp(`${sel}[^{]*\\{[^}]*${decl}`), `${sel} { ${decl} }`);
	rule('\\.wslot\\.armed', 'border-color: ?var\\(--es-warn\\)');
	rule(
		'\\.wslot\\.armed',
		'animation: ?[\\w-]*pulse[\\w-]* 0\\.45s ease-in-out infinite alternate'
	);
	rule('\\.wslot\\.playing', 'background: ?var\\(--es-playing-bg\\)');
	rule('\\.wslot\\.playing[^{]*\\.edge', 'box-shadow: ?0 0 12px var\\(--es-accent\\)');
	rule('\\.wslot\\.editing', 'outline: ?2px solid var\\(--es-deck-a\\)');
	rule('\\.wslot\\.playing[^{]*\\.ph', 'background: ?var\\(--es-playhead\\)');
	rule('\\.wslot\\.playing[^{]*\\.lb', 'color: ?var\\(--es-accent\\)');
	rule('\\.wslot\\.previewing[^{]*\\.ph', 'background: ?var\\(--es-deck-a\\)');
	rule('\\.wslot\\.previewing[^{]*\\.wv', 'outline: ?1px solid var\\(--es-deck-a\\)');
	assert.match(
		css,
		/prefers-reduced-motion: ?reduce\)\s*\{[^}]*\.wslot\.armed[^{]*\{[^}]*animation: ?none/
	);
	assert.match(css, /box-shadow: ?0 0 0 4px var\(--es-armed-glow\)/);
	assert.deepEqual(css.match(/#[0-9a-f]{3,8}\b|rgba?\(/gi) ?? [], []);
	assert.deepEqual(css.match(/var\(--rb-[\w-]+\)/g) ?? [], []);
});

test('a non-match or a tab beside the edited one settles to the awake strip', () => {
	/** [if] tab 2 does not match and tab 5 is beside the edited tab [then] both ease to 32 px, the match pops, [else stop]. */
	const n = 20;
	const m = wave.createEdgeMotion(n);
	m.awake = true;
	const matches = Array(n).fill(true);
	matches[2] = false;
	const editingOther = Array(n).fill(false);
	editingOther[5] = true;
	const tabY = (i) => 28 + 132 + 4 + i * 32 + 16;
	const frame = (pointerY, o = {}) => ({
		enabled: true,
		awake: true,
		armed: true,
		fromEdgePx: 8,
		pointerY,
		top: 28,
		headH: 132,
		tabH: 32,
		tabCount: n,
		buttons: 0,
		inWindow: true,
		t: 5000,
		appDrag: false,
		held: Array(n).fill(false),
		filled: Array(n).fill(true),
		matches,
		editingOther,
		params: wave.DEFAULT_WAVE,
		reduceMotion: true,
		dtMs: 16,
		headW: m.headW,
		widths: m.widths,
		...o
	});
	wave.stepEdgeMotion(m, frame(tabY(2)));
	assert.equal(m.widths[2], wave.AWAKE_W);
	wave.stepEdgeMotion(m, frame(tabY(5)));
	assert.equal(m.widths[5], wave.AWAKE_W);
	wave.stepEdgeMotion(m, frame(tabY(9), { searchOpen: true }));
	assert.equal(m.widths[9], wave.DEFAULT_WAVE.pop);
	assert.equal(m.widths[15], wave.DEFAULT_WAVE.pop);
	assert.equal(m.widths[2], wave.AWAKE_W);
});
