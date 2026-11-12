import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ui;
let stemGraph;

before(async () => {
	ui = await loadTypeScriptModule('src/lib/components/rb/wave/stem-waveform-ui.ts');
	stemGraph = await loadTypeScriptModule('src/lib/rb/stem-graph.ts');
});

function neutral() {
	return {
		vocal: { muted: false, solo: false, gain: 0.5 },
		instrumental: { muted: false, solo: false, gain: 0.5 },
		drums: { muted: false, solo: false, gain: 0.5 },
		bass: { muted: false, solo: false, gain: 0.5 },
		other: { muted: false, solo: false, gain: 0.5 }
	};
}

const STEMS = ['vocal', 'instrumental', 'drums', 'bass', 'other'];

test('if all stem controls are neutral then every row is audible', () => {
	const controls = neutral();
	for (const stem of STEMS) {
		assert.equal(ui.stemRowAudibility(controls, stem, 'stems', 'demucs4'), 'audible', stem);
	}
});

test('if VOCAL is muted then vocal is muted and the rest stay audible', () => {
	const controls = neutral();
	controls.vocal.muted = true;
	assert.equal(ui.stemRowAudibility(controls, 'vocal', 'stems'), 'muted');
	for (const stem of STEMS.filter((s) => s !== 'vocal')) {
		assert.equal(ui.stemRowAudibility(controls, stem, 'stems'), 'audible', stem);
	}
});

test('if DRUMS is soloed then drums is audible and the rest are silenced_by_solo', () => {
	const controls = neutral();
	controls.drums.solo = true;
	assert.equal(ui.stemRowAudibility(controls, 'drums', 'stems'), 'audible');
	for (const stem of STEMS.filter((s) => s !== 'drums')) {
		assert.equal(ui.stemRowAudibility(controls, stem, 'stems'), 'silenced_by_solo', stem);
	}
});

test('if DRUMS is soloed and muted then drums is muted and the rest are silenced_by_solo', () => {
	const controls = neutral();
	controls.drums.solo = true;
	controls.drums.muted = true;
	assert.equal(ui.stemRowAudibility(controls, 'drums', 'stems'), 'muted');
	for (const stem of STEMS.filter((s) => s !== 'drums')) {
		assert.equal(ui.stemRowAudibility(controls, stem, 'stems'), 'silenced_by_solo', stem);
	}
});

test('if playback is original then every stem is audible whatever the controls say', () => {
	const controls = neutral();
	controls.vocal.muted = true;
	controls.drums.solo = true;
	for (const stem of STEMS) {
		assert.equal(ui.stemRowAudibility(controls, stem, 'original'), 'audible', stem);
	}
});

test('if demucs4 instrumental is muted then bass and other are muted', () => {
	const controls = neutral();
	controls.instrumental.muted = true;
	assert.equal(ui.stemRowAudibility(controls, 'bass', 'stems'), 'muted');
	assert.equal(ui.stemRowAudibility(controls, 'other', 'stems'), 'muted');
	assert.equal(ui.stemRowAudibility(controls, 'vocal', 'stems'), 'audible');
});

test('if demucs4 instrumental is soloed then instrumental bass and other are audible and the rest silenced', () => {
	const controls = neutral();
	controls.instrumental.solo = true;
	assert.equal(ui.stemRowAudibility(controls, 'instrumental', 'stems'), 'audible');
	assert.equal(ui.stemRowAudibility(controls, 'bass', 'stems'), 'audible');
	assert.equal(ui.stemRowAudibility(controls, 'other', 'stems'), 'audible');
	assert.equal(ui.stemRowAudibility(controls, 'vocal', 'stems'), 'silenced_by_solo');
	assert.equal(ui.stemRowAudibility(controls, 'drums', 'stems'), 'silenced_by_solo');
});

test('if roformer2 has DRUMS solo set then vocal and instrumental stay audible', () => {
	const controls = neutral();
	controls.drums.solo = true;
	assert.equal(ui.stemRowAudibility(controls, 'vocal', 'stems', 'roformer2'), 'audible');
	assert.equal(ui.stemRowAudibility(controls, 'instrumental', 'stems', 'roformer2'), 'audible');
});

test('stemWaveRowAlpha maps audibility to paint alpha', () => {
	assert.equal(ui.stemWaveRowAlpha('audible'), 1);
	assert.equal(ui.stemWaveRowAlpha('muted'), ui.STEM_WAVE_ROW_SILENT_ALPHA);
	assert.equal(ui.stemWaveRowAlpha('silenced_by_solo'), ui.STEM_WAVE_ROW_SILENT_ALPHA);
});

const DEMUCS_PARTS = [
	['vocal', 'vocals'],
	['drums', 'drums'],
	['bass', 'bass'],
	['other', 'other']
];

function muteSoloGrid() {
	const grids = [];
	for (let mask = 0; mask < 1 << 10; mask += 1) {
		const controls = neutral();
		const stems = ['vocal', 'instrumental', 'drums', 'bass', 'other'];
		for (let i = 0; i < 10; i += 1) {
			const stem = stems[Math.floor(i / 2)];
			if (i % 2 === 0) controls[stem].muted = Boolean(mask & (1 << i));
			else controls[stem].solo = Boolean(mask & (1 << i));
		}
		grids.push(controls);
	}
	return grids;
}

test('if stemRowAudibility disagrees with stemPartGains on demucs4 then broken', () => {
	for (const controls of muteSoloGrid()) {
		const gains = stemGraph.stemPartGains(controls, 'demucs4');
		for (const [stem, part] of DEMUCS_PARTS) {
			const audible = ui.stemRowAudibility(controls, stem, 'stems', 'demucs4') === 'audible';
			const heard = (gains[part] ?? 0) > 0;
			assert.equal(audible, heard, JSON.stringify({ stem, controls }));
		}
	}
});

test('if stemRowAudibility disagrees with stemPartGains on roformer2 then broken', () => {
	const roformerParts = [
		['vocal', 'vocals'],
		['instrumental', 'instrumental']
	];
	for (const controls of muteSoloGrid()) {
		const gains = stemGraph.stemPartGains(controls, 'roformer2');
		for (const [stem, part] of roformerParts) {
			const audible = ui.stemRowAudibility(controls, stem, 'stems', 'roformer2') === 'audible';
			const heard = (gains[part] ?? 0) > 0;
			assert.equal(audible, heard, JSON.stringify({ stem, controls }));
		}
	}
});

const REPAINT_HARNESS = [
	"import { flushSync } from 'svelte';",
	"import { stemRowAudibility, stemWaveRowAlpha } from '$lib/components/rb/wave/stem-waveform-ui';",
	"import { drawStemWaveRow } from '$lib/components/rb/wave/render';",
	'const STEMS = ["vocal", "instrumental", "drums", "bass", "other"] as const;',
	'function neutral() {',
	'	return { vocal: { muted: false, solo: false, gain: 0.5 }, instrumental: { muted: false, solo: false, gain: 0.5 }, drums: { muted: false, solo: false, gain: 0.5 }, bass: { muted: false, solo: false, gain: 0.5 }, other: { muted: false, solo: false, gain: 0.5 } };',
	'}',
	'function fakeCtx() { return { globalAlpha: 1, fillStyle: "", clearRect() {}, fillRect() {} }; }',
	'const frame = { envelope: [0, 1, 0], scrollPx: 0, windowS: 24, playheadFrac: 0.5, durationMs: 3000, pitch: 1, width: 100, height: 12, color: "#fff" };',
	'export function mountRows() {',
	'	let stems = $state({ controls: neutral(), playback: "stems" as const, layout: "demucs4" as const });',
	'	const draws: Record<string, number> = { vocal: 0, instrumental: 0, drums: 0, bass: 0, other: 0 };',
	'	const alphas: Record<string, number> = { vocal: 1, instrumental: 1, drums: 1, bass: 1, other: 1 };',
	'	const ctxs = Object.fromEntries(STEMS.map((s) => [s, fakeCtx()]));',
	'	function row(stem: (typeof STEMS)[number]) {',
	'		const aud = $derived(stemRowAudibility(stems.controls, stem, stems.playback, stems.layout));',
	'		$effect(() => {',
	'			const alpha = stemWaveRowAlpha(aud);',
	'			alphas[stem] = alpha;',
	'			draws[stem] += 1;',
	'			drawStemWaveRow(ctxs[stem] as CanvasRenderingContext2D, { ...frame, alpha });',
	'		});',
	'	}',
	'	const stop = $effect.root(() => { for (const stem of STEMS) row(stem); flushSync(); });',
	'	return {',
	'		draws,',
	'		alphas,',
	'		stop,',
	'		setControls(next: ReturnType<typeof neutral>) { stems = { ...stems, controls: next }; flushSync(); },',
	'		setPlayback(p: "original" | "stems") { stems = { ...stems, playback: p }; flushSync(); }',
	'	};',
	'}'
].join('\n');

test('if muting one stem repaints only rows whose audibility changed then ok', async () => {
	const live = await loadRuneModule(REPAINT_HARNESS);
	const rows = live.mountRows();
	for (const stem of STEMS) {
		assert.equal(rows.draws[stem], 1, `mount draw ${stem}`);
	}
	const next = neutral();
	next.vocal.muted = true;
	rows.setControls(next);
	assert.equal(rows.draws.vocal, 2, 'vocal audibility changed');
	assert.equal(rows.alphas.vocal, ui.STEM_WAVE_ROW_SILENT_ALPHA);
	for (const stem of STEMS.filter((s) => s !== 'vocal')) {
		assert.equal(rows.draws[stem], 1, `${stem} unchanged after vocal mute`);
	}

	const solo = { ...next, drums: { ...next.drums, solo: true } };
	rows.setControls(solo);
	assert.equal(rows.draws.drums, 1, 'drums still audible');
	assert.equal(rows.draws.vocal, 2, 'vocal still muted');
	for (const stem of ['instrumental', 'bass', 'other']) {
		assert.equal(rows.draws[stem], 2, `${stem} silenced by drums solo`);
		assert.equal(rows.alphas[stem], ui.STEM_WAVE_ROW_SILENT_ALPHA);
	}

	rows.setPlayback('original');
	const drumsBefore = rows.draws.drums;
	for (const stem of ['vocal', 'instrumental', 'bass', 'other']) {
		assert.equal(rows.draws[stem], 3, `${stem} brightens on original playback`);
		assert.equal(rows.alphas[stem], 1);
	}
	assert.equal(rows.draws.drums, drumsBefore, 'drums was already audible');
	rows.stop();
});
