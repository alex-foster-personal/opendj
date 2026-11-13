// requirement: EDGESAMP-14395, EDGESAMP-14401, EDGESAMP-14420
/**
 * Issue #11126 (S8b): the edit modal opened by a tab's pencil (spec 7.4, mock `openSlotPop`):
 * name and description behind pencils, the source line, the CUE-only deck with its beatgrid,
 * the captured mix chips and the explainer row.
 *
 * Regression lines:
 * - if a rename hides the source track then the user loses where the sample came from
 * - if Escape in the name input saves then a cancelled rename still changes the tab
 * - if the CUE deck shows a ready button with no audio engine then a press looks like it played
 * - if the modal opens over the tab or other tabs stay popped then the edited tab is hidden
 * - if the modal's elements, order or tokens drift from the mock then it stops looking like it
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ssr;
let edit;
let draw;
before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as EdgeWave } from '$lib/components/rb/sampler/EdgeWave.svelte';",
			"export { DEFAULT_WAVE, HEAD_OPEN_W, tabTargetWidth } from '$lib/player/sampler/edge-wave';",
			"export * as ui from '$lib/player/sampler/sampler-store.svelte';",
			"export { NO_ENGINE } from '$lib/player/sampler/sampler-engine-seam';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{ css: 'injected' }
	);
	edit = await loadTypeScriptModule('src/lib/player/sampler/sample-edit.ts');
	draw = await loadTypeScriptModule('src/lib/components/rb/sampler/region-wave-draw.ts');
});

const UNITY = { gain_db: 0, mute: false, solo: false };
const VECTOR = {
	trim_db: -6,
	eq_db: { low: -12, mid: 0, high: 0 },
	filter: 0.18,
	stems: {
		vocals: { gain_db: 0, mute: true, solo: false },
		drums: { gain_db: 3, mute: false, solo: false },
		bass: UNITY,
		inst: UNITY
	},
	key_shift_st: 0,
	master_tempo: true
};
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
	peaks: { low: [0.5], mid: [0.4], high: [0.3] },
	stems_ready: false,
	stem_parts: null,
	...o
});

/** Load `slots` onto the R edge through a fetch that answers like the sampler routes (PATCH echoes). */
async function load(slots) {
	const json = (body) => new Response(JSON.stringify(body), { status: 200 });
	ssr.ui.setSamplerFetch(async (url, init) => {
		const u = new URL(String(url));
		if (init?.method === 'PATCH') {
			const id = u.pathname.split('/').pop();
			const cur = ssr.ui.sampler.slots.find((s) => s.slot_id === id);
			return json({ ...cur, ...JSON.parse(String(init.body)) });
		}
		if (u.pathname.endsWith('/slots'))
			return json({ slots, total: slots.length, skipped_no_grid: 0, rekordbox: 'ok' });
		return json(u.pathname.endsWith('/sets') ? { sets: [] } : { pins: [] });
	});
	await ssr.ui.loadSampler();
	ssr.ui.setEdgeSet('R', 0);
}

/** Server-render the R edge; returns its markup, the modal's markup and all scoped CSS. */
function renderEdge() {
	const props = {
		edge: 'R',
		top: 28,
		height: 900,
		params: ssr.DEFAULT_WAVE,
		enabled: true,
		reduceMotion: true
	};
	const { head, body } = ssr.render(ssr.EdgeWave, { props });
	const css = [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
	const at = body.indexOf('data-testid="edit-sample-modal"');
	return { body, modal: at < 0 ? '' : body.slice(body.lastIndexOf('<div', at)), css };
}

const tabOf = (body, id) =>
	body.split(/(?=<div[^>]*class="wslot)/).find((t) => t.includes(`data-slot-id="${id}"`)) ?? '';

test('a renamed sample shows its new name on the tab and its source track in the modal', async () => {
	/** [if] s0 is renamed through updateSlot with the modal open [then] tab and modal read Big Drop, source still Track 0, [else stop]. */
	await load([slot(0), slot(1)]);
	ssr.ui.openSlotEditor('s0', 'R');
	await ssr.ui.updateSlot('s0', { name: 'Big Drop', description: 'Riser' });
	const { body, modal } = renderEdge();
	assert.match(tabOf(body, 's0'), />Big Drop</);
	assert.match(modal, /class="val name[^"]*">Big Drop</);
	assert.match(modal, /class="val desc[^"]*">Riser</);
	assert.match(
		modal,
		/Source: <b[^>]*>Track 0<\/b> \. Artist \. hot cue A \. 4 bars \. 124\.0 BPM 8A/
	);
	assert.deepEqual(edit.sourceLine(slot(0, { name: 'Big Drop' })).title, 'Track 0');
	ssr.ui.openSlotEditor(null);
});

test('Escape in a name input cancels, Enter saves, and only a real change is sent', () => {
	/** [if] Escape is pressed in the name input [then] the action is cancel and no patch exists, [else stop]. */
	assert.equal(edit.editKeyAction('name', 'Escape'), 'cancel');
	assert.equal(edit.editKeyAction('description', 'Escape'), 'cancel');
	assert.equal(edit.editKeyAction('name', 'Enter'), 'save');
	assert.equal(edit.editKeyAction('description', 'Enter'), null);
	const s = slot(0, { name: 'Old' });
	assert.equal(edit.editPatch(s, 'name', ' Old '), null);
	assert.deepEqual(edit.editPatch(s, 'name', 'New'), { name: 'New' });
	assert.deepEqual(edit.editPatch(s, 'name', 'Track 0'), { name: null });
	assert.deepEqual(edit.editPatch(s, 'name', '  '), { name: null });
	assert.equal(edit.editPatch(s, 'description', ''), null);
	assert.deepEqual(edit.editPatch(s, 'description', ' Riser '), { description: 'Riser' });
});

test('the edited tab stays out with an outline, the modal sits beside it, other tabs settle', async () => {
	/** [if] s1's pencil opens the modal [then] s1 is held and outlined, s0 settles to its base width, modal is 486 px in, [else stop]. */
	await load([slot(0), slot(1)]);
	ssr.ui.openSlotEditor('s1', 'R');
	const { body, modal, css } = renderEdge();
	assert.match(tabOf(body, 's1'), /class="wslot[^"]*\bheld\b[^"]*\bediting\b/);
	assert.doesNotMatch(tabOf(body, 's0'), /\bediting\b/);
	assert.match(modal, new RegExp(`right: ${ssr.HEAD_OPEN_W + 10}px`));
	assert.match(css, /\.wslot\.editing[^{]*\{[^}]*outline: ?2px solid var\(--es-deck-a\)/);
	const other = { filled: true, awake: true, held: false, editingOther: true, dy: 0, tabH: 40 };
	assert.equal(ssr.tabTargetWidth({ ...other, ramp: 1 }, ssr.DEFAULT_WAVE) < 100, true);
	ssr.ui.openSlotEditor(null);
	assert.equal(renderEdge().modal, '');
});

test('the modal carries the mock elements in order, with mix chips and tokens', async () => {
	/** [if] a capture with a mix opens [then] title, name, desc, source, CUE deck, chips, info, explainer run in mock order, [else stop]. */
	await load([slot(0, { source: 'capture', cue_letter: null, vector: VECTOR })]);
	ssr.ui.openSlotEditor('s0', 'R');
	const { modal, css } = renderEdge();
	const order = [
		/role="dialog"[^>]*aria-label="Edit sample"/,
		/<h3[^>]*>Edit sample<\/h3>/,
		/class="val name/,
		/aria-label="Edit sample name"/,
		/class="val desc[^"]*empty[^"]*">Add a description</,
		/aria-label="Edit description"/,
		/class="src/,
		/class="cuedeck/,
		/<canvas/,
		/class="vec/,
		/Native 124\.0 BPM 8A\. Plays at its own BPM in 8A, on its own beatgrid\./,
		/class="explain[^"]*idle[^"]*">Hover a setting to see what it does\.</
	];
	const pos = order.map((re) => modal.search(re));
	assert.ok(
		pos.every((p) => p >= 0),
		`all present: ${pos}`
	);
	assert.deepEqual(
		[...pos].sort((a, b) => a - b),
		pos,
		'mock DOM order'
	);
	assert.match(modal, /deck loop, dragged in/);
	assert.match(modal, />Vocals muted \(pending\)</);
	assert.match(modal, /<span[^>]*>EQ low -12 dB<\/span>/);
	const rule = (sel, decl) =>
		assert.match(css, new RegExp(`${sel}[^{]*\\{[^}]*${decl}`), `${sel} { ${decl} }`);
	rule('\\.pop', 'width: ?600px');
	rule('\\.val\\.name', 'font: ?22px var\\(--es-font-display\\)');
	rule('\\.src', 'background: ?var\\(--es-source-bg\\)');
	rule('\\.cuedeck', 'border: ?1px solid var\\(--es-cuedeck-border\\)');
	rule('\\.cwrap', 'height: ?84px');
	rule('\\.cuebtn\\[aria-pressed=.true.\\]', 'color: ?var\\(--es-cue-ink\\)');
	rule('\\.vec[^{]*span', 'border: ?1px solid var\\(--es-vec-border\\)');
	const modalCss = css.slice(css.indexOf('.pop.svelte-'));
	assert.deepEqual(modalCss.match(/var\(--rb-[\w-]+\)/g) ?? [], []);
	assert.deepEqual(edit.vectorChips(VECTOR), [
		'Trim -6 dB',
		'EQ low -12 dB',
		'Filter HP 18%',
		'Vocals muted',
		'Drums +3 dB',
		'Master Tempo on'
	]);
	ssr.ui.openSlotEditor(null);
});

test('with no audio engine the CUE deck says it is not ready and CUE is disabled', async () => {
	/** [if] the modal opens before an engine registers [then] CUE is disabled and a not-ready status shows, [else stop]. */
	await load([slot(0)]);
	assert.equal(ssr.ui.sampler.engineReady, false);
	ssr.ui.openSlotEditor('s0', 'R');
	const { modal } = renderEdge();
	assert.match(modal, /<button[^>]*class="cuebtn[^"]*"[^>]*disabled/);
	assert.match(
		modal,
		/data-testid="cue-deck-not-ready"[^>]*>Not ready: the sampler's audio engine/
	);
	assert.doesNotMatch(modal, /Headphones only/);
	ssr.ui.openSlotEditor(null);
});

test('CUE previews on the cue bus only, and shows pause while previewing', async () => {
	/** [if] an engine registers and CUE runs [then] only engine.preview is called and the pressed CUE shows pause, [else stop]. */
	await load([slot(0)]);
	const calls = [];
	let phase = 'free';
	ssr.ui.registerSamplerEngine({
		toggle: async (s) => void calls.push(`toggle ${s.slot_id}`),
		stop: () => undefined,
		preview: async (s) => void calls.push(`preview ${s.slot_id}`),
		stopPreview: () => void calls.push('stopPreview'),
		warm: () => undefined,
		phase: () => phase
	});
	try {
		await ssr.ui.togglePreview(ssr.ui.sampler.slots[0], false);
		assert.deepEqual(calls, ['preview s0']);
		phase = 'previewing';
		ssr.ui.openSlotEditor('s0', 'R');
		const { modal } = renderEdge();
		assert.match(modal, /class="cuebtn[^"]*"[^>]*aria-pressed="true"[^>]*>(<!--\[0-->)?❚❚ CUE/);
		assert.match(modal, /Headphones only \. no MASTER deck, plays at its own BPM, plays in 8A/);
		assert.match(modal, /class="cph[^"]*\bon\b/);
	} finally {
		ssr.ui.openSlotEditor(null);
		ssr.ui.registerSamplerEngine(ssr.NO_ENGINE);
	}
});

test('the CUE deck beatgrid ticks every beat with brighter downbeats that follow the offset', () => {
	/** [if] a 16 beat region draws with downbeat offset 1 [then] 17 ticks, beats 1, 5, 9, 13 are 2 px at .85, [else stop]. */
	const ticks = draw.beatTicks(16, 400, 1);
	assert.equal(ticks.length, 17);
	assert.deepEqual(
		ticks.flatMap((t, b) => (t.down ? [b] : [])),
		[1, 5, 9, 13]
	);
	assert.deepEqual([ticks[1].w, ticks[1].alpha, ticks[2].w, ticks[2].alpha], [2, 0.85, 1, 0.3]);
	assert.equal(ticks[16].x, 398);
	assert.deepEqual(draw.beatTicks(0, 400), []);
	assert.deepEqual(
		draw.beatTicks(8, 100, -1).flatMap((t, b) => (t.down ? [b] : [])),
		[3, 7]
	);
});

test('the settings rows read in the deck words and mock order, with the current choice pressed', async () => {
	/** [if] a One shot slot nudged +1 beat opens [then] six rows in mock order, One shot pressed, "+1 beat" shown, [else stop]. */
	const settings = { ...slot(0).settings, mode: 'one_shot', repeat: 'once', downbeat_offset: 1 };
	await load([slot(0, { settings })]);
	ssr.ui.openSlotEditor('s0', 'R');
	const { modal, css } = renderEdge();
	const order = [
		/class="sum[^"]*">Native/,
		/class="grid2/,
		/>Launch &amp; sync to</,
		/>Next bar</,
		/>Next beat</,
		/>4 bars</,
		/>Off \(no sync\)</,
		/class="lbl[^"]*"[^>]*>Key</,
		/>KEY SYNC</,
		/>Master Tempo</,
		/>Master Tempo OFF</,
		/class="lbl[^"]*"[^>]*>Tempo</,
		/>Match MASTER</,
		/>Own BPM</,
		/class="lbl[^"]*"[^>]*>Mode</,
		/>Loop</,
		/>One shot</,
		/>Gate</,
		/>Downbeat</,
		/aria-label="One beat earlier"/,
		/>\+1 beat</,
		/aria-label="One beat later"/,
		/>Level</,
		/type="range"[^>]*min="0"[^>]*max="100"/,
		/class="explain/
	];
	const pos = order.map((re) => modal.search(re));
	assert.ok(
		pos.every((p) => p >= 0),
		`all present: ${pos}`
	);
	assert.deepEqual(
		[...pos].sort((a, b) => a - b),
		pos,
		'mock DOM order'
	);
	assert.match(modal, /aria-pressed="true"[^>]*>One shot</);
	assert.match(modal, /aria-pressed="false"[^>]*>Loop</);
	assert.match(modal, /aria-pressed="true"[^>]*>Next bar</);
	assert.match(modal, /data-tip="Master Tempo ON - hold musical key while changing tempo"/);
	const rule = (sel, decl) =>
		assert.match(css, new RegExp(`${sel}[^{]*\\{[^}]*${decl}`), `${sel} { ${decl} }`);
	rule('\\.grid2', 'grid-template-columns: ?1fr 1fr');
	rule('\\.seg[^{]*button\\[aria-pressed=.true.\\]', 'color: ?var\\(--es-accent-ink\\)');
	rule('\\.lbl', 'font: ?13px var\\(--es-font-display\\)');
	ssr.ui.openSlotEditor(null);
});

test('a settings change sends the full settings, One shot plays once and the downbeat clamps', async () => {
	/** [if] Mode goes One shot then Loop, downbeat past 3 [then] repeat once then loop, offset stays 3, PATCH has full settings, [else stop]. */
	const base = slot(0).settings;
	const once = edit.settingsWith(base, 'mode', 'one_shot');
	assert.deepEqual([once.mode, once.repeat], ['one_shot', 'once']);
	const back = edit.settingsWith(once, 'mode', 'loop');
	assert.deepEqual([back.mode, back.repeat], ['loop', 'loop']);
	assert.equal(edit.settingsWith({ ...base, repeat: 4 }, 'mode', 'gate').repeat, 4);
	assert.equal(
		edit.settingsWith({ ...base, downbeat_offset: 3 }, 'downbeat_offset', 4).downbeat_offset,
		3
	);
	assert.equal(edit.settingsWith(base, 'level', 140).level, 100);
	assert.deepEqual([edit.downbeatLabel(0), edit.downbeatLabel(-2)], ['on grid', '-2 beat']);
	await load([slot(0)]);
	const sent = [];
	ssr.ui.setSamplerFetch(async (_url, init) => {
		sent.push(JSON.parse(String(init.body)));
		return new Response(JSON.stringify({ ...slot(0), ...sent.at(-1) }), { status: 200 });
	});
	const saved = await ssr.ui.updateSlot('s0', { settings: once });
	assert.deepEqual(Object.keys(sent[0].settings).sort(), Object.keys(base).sort());
	assert.equal(saved.settings.repeat, 'once');
});
