// REQ: MIXUX-04 (on-screen swap superseded by MIXUX-14, Mon 2 Nov 2026; the
// fixed map below now drives only a MIDI controller's EQ knobs in stem_eq_mode)
// [if] only vocal and non-vocal stems are available [then] HI -> vocal, MED -> non-vocal, and LO stays LO (any dial with no stem available keeps its EQ function).
// [if] a dial is a stem dial [then] it is color coded per the repo's stem color rules (eg vocals blue) [⛔️ if STEM dials render indistinguishable from EQ dials].

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { compile } from 'svelte/compiler';

import { loadTypeScriptModule } from './load-typescript.mjs';

async function source(relative) {
	return readFile(`src/lib/components/rb/${relative}`, 'utf8');
}

test('MIXUX-14 supersedes the on-screen swap: the mixer sends stem_gain and no strip carries a STEM mode button', async () => {
	const strip = await source('mixer/ChannelStrip.svelte');
	assert.doesNotThrow(() => compile(strip, { filename: 'ChannelStrip.svelte', generate: 'server' }));
	assert.doesNotMatch(strip, /stem-mode-channel-/);
	assert.doesNotMatch(strip, /onStemEqMode/);
	assert.match(strip, /stemDialLayout\(deck\.stems\.available_controls\)/);

	const mixer = await source('Mixer.svelte');
	assert.match(mixer, /\{ type: 'stem_gain', deck, stem, value \}/);
	assert.doesNotMatch(mixer, /type: 'stem_eq_mode'/);

	// stem_eq_mode stays a command: a MIDI controller's EQ knobs still follow it.
	const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await ipc.dispatchPerformanceCommand({ type: 'stem_eq_mode', deck: 1, enabled: true });
		assert.equal(ipc.queryPerformanceState().mixer.channels[1].stem_eq_mode, true);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('MIDI EQ mode keeps the fixed HI/MID/LOW -> vocal/inst/drums map; a missing stem keeps its EQ', async () => {
	const map = await loadTypeScriptModule('src/lib/rb/stem-dial-map.ts');
	assert.deepEqual(map.stemDialAssignment(['vocal', 'instrumental', 'drums']), {
		high: 'vocal',
		mid: 'instrumental',
		low: 'drums'
	});
	assert.deepEqual(map.stemDialAssignment(['vocal', 'instrumental']), {
		high: 'vocal',
		mid: 'instrumental',
		low: null
	});
	assert.deepEqual(map.stemDialAssignment([]), { high: null, mid: null, low: null });
	assert.equal(map.STEM_DIAL_LABELS.vocal, 'VOCAL');
	assert.equal(map.STEM_DIAL_LABELS.instrumental, 'INST');
	assert.equal(map.STEM_DIAL_LABELS.drums, 'DRUMS');
	assert.equal(map.STEM_DIAL_LABELS.bass, 'BASS');
});

test('STEM dials use stem colors distinct from default EQ accent', async () => {
	const colors = await loadTypeScriptModule('src/lib/rb/stem-colors.ts');
	const wave = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	assert.equal(colors.STEM_COLORS.vocal, wave.VOCAL_BLUE);

	const row = await source('deck/StemRow.svelte');
	assert.match(row, /stemCssColor\(stem\.id\)/);
	assert.doesNotMatch(row, /--rb-green/);

	const knob = await source('mixer/Knob.svelte');
	assert.match(knob, /accentColor/);
	assert.match(knob, /knob-stem-accent/);
	assert.match(knob, /if \(accentColor\) return 'none'/);

	const strip = await source('mixer/ChannelStrip.svelte');
	// Every stem dial is drawn in its stem's colour (MIXUX-14 snippet).
	assert.match(strip, /accentColor=\{stemCssColor\(stem\)\}/);
	assert.match(strip, /stemCssColor\(stem\)/);
});
