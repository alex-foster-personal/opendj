// REQ: MIXUX-04
// [if] the per-channel STEM button below the channel fader is engaged [then] that channel's EQ dials switch to controlling STEM sub-channels, with labels auto-populated from the stems available for the loaded track.
// [if] only vocal and non-vocal stems are available [then] HI -> vocal, MED -> non-vocal, and LO stays LO (any dial with no stem available keeps its EQ function).
// [if] a dial is in STEM mode [then] it is color coded per the repo's stem color rules (eg vocals blue) [⛔️ if STEM dials render indistinguishable from EQ dials].

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { compile } from 'svelte/compiler';

import { loadTypeScriptModule } from './load-typescript.mjs';

async function source(relative) {
	return readFile(`src/lib/components/rb/${relative}`, 'utf8');
}

test('STEM button engaged remaps labels from available stems', async () => {
	const strip = await source('mixer/ChannelStrip.svelte');
	assert.doesNotThrow(() => compile(strip, { filename: 'ChannelStrip.svelte', generate: 'server' }));
	assert.match(
		strip,
		/<div class="fader-slot">[\s\S]*?<\/div>\s*<button[\s\S]*class="stem-label"[\s\S]*data-testid=\{`stem-mode-channel-\$\{deckId\}`\}[\s\S]*<div class="stem-slot">\s*<StemRow/,
		'StemRow must sit below the STEM mode button under the fader'
	);
	assert.match(strip, /stemDialAssignment/);
	assert.match(strip, /STEM_DIAL_LABELS\[stem\]/);

	const map = await loadTypeScriptModule('src/lib/rb/stem-dial-map.ts');
	const allThree = map.stemDialAssignment(['vocal', 'instrumental', 'drums']);
	assert.deepEqual(allThree, { high: 'vocal', mid: 'instrumental', low: 'drums' });
	assert.equal(map.STEM_DIAL_LABELS.vocal, 'VOCAL');
	assert.equal(map.STEM_DIAL_LABELS.instrumental, 'INST');
	assert.equal(map.STEM_DIAL_LABELS.drums, 'DRUMS');

	const mixer = await source('Mixer.svelte');
	assert.match(mixer, /type: 'stem_eq_mode'/);
	assert.match(mixer, /type: 'stem_gain'/);
	assert.match(mixer, /\{ type: 'stem_eq_mode', deck, enabled \}/);
	assert.match(mixer, /\{ type: 'stem_gain', deck, stem, value \}/);

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

test('roformer2 vocal and non-vocal keeps LO as EQ', async () => {
	const map = await loadTypeScriptModule('src/lib/rb/stem-dial-map.ts');
	assert.deepEqual(map.stemDialAssignment(['vocal', 'instrumental']), {
		high: 'vocal',
		mid: 'instrumental',
		low: null
	});
	assert.deepEqual(map.stemDialAssignment([]), { high: null, mid: null, low: null });
	assert.deepEqual(map.stemDialAssignment(['vocal']), { high: 'vocal', mid: null, low: null });

	const strip = await source('mixer/ChannelStrip.svelte');
	assert.match(strip, /stemDialAssignment/);
	assert.match(strip, /label={lowDial\.label}/);
	assert.match(strip, /oneq\(band, value\)/);
});

test('STEM dials use stem colors distinct from default EQ accent', async () => {
	const colors = await loadTypeScriptModule('src/lib/rb/stem-colors.ts');
	const wave = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	assert.equal(colors.STEM_COLORS.vocal, wave.VOCAL_BLUE);

	const row = await source('deck/StemRow.svelte');
	assert.match(row, /STEM_COLORS\.vocal/);
	assert.doesNotMatch(row, /--rb-green/);

	const knob = await source('mixer/Knob.svelte');
	assert.match(knob, /accentColor/);
	assert.match(knob, /knob-stem-accent/);
	assert.match(knob, /if \(accentColor\) return 'none'/);

	const strip = await source('mixer/ChannelStrip.svelte');
	// 3504858fa (MIXUX-04) passes accentColor as a conditional spread so an
	// EQ dial never receives an explicit accentColor={undefined}
	// (exactOptionalPropertyTypes); a STEM dial still gets its stem color.
	for (const dial of ['hiDial', 'midDial', 'lowDial']) {
		assert.match(
			strip,
			new RegExp(`\\{\\.\\.\\.\\(${dial}\\.accentColor \\? \\{ accentColor: ${dial}\\.accentColor \\} : \\{\\}\\)\\}`),
			`${dial} must forward its stem accentColor to Knob`
		);
	}
	assert.match(strip, /STEM_COLORS\[stem\]/);
});
