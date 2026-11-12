/**
 * #9435 (epic #9371, MX-2): SideMixerStrip source contract.
 *
 * Regression lines:
 * - if SideMixerStrip.svelte does not compile then broken
 * - if it does not build from Knob, VFader and ChannelLevelMeter then broken
 * - if it does not read mixerState.channels[deckId] then it holds a second copy of the state
 * - if it declares $state( then it holds a second copy of the state
 * - if the side prop is not exactly 'outer-left' | 'outer-right' then broken
 * - if the column order is not one list reversed for outer-right then broken
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { compile } from 'svelte/compiler';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SIDE_MIXER_STRIP = 'src/lib/components/rb/mixer/SideMixerStrip.svelte';

const SSR_ENTRY = [
	"export { default as SideMixerStrip } from '$lib/components/rb/mixer/SideMixerStrip.svelte';",
	"export { deckStates, mixerState } from '$lib/player/state.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let ssr;

before(async () => {
	// SSR runs ChannelLevelMeter onDestroy (meter rAF teardown); node has no rAF.
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	ssr = await loadSvelteSsrModule(SSR_ENTRY);
});

async function sideMixerSource() {
	return readFile(SIDE_MIXER_STRIP, 'utf8');
}

test('side mixer strip compiles', async () => {
	const src = await sideMixerSource();
	assert.doesNotThrow(
		() => compile(src, { filename: 'SideMixerStrip.svelte', generate: 'server' }),
		'if SideMixerStrip.svelte does not compile then broken'
	);
});

test('side mixer strip is built from the shared mixer controls', async () => {
	const src = await sideMixerSource();
	for (const name of ['Knob', 'VFader', 'ChannelLevelMeter']) {
		assert.match(
			src,
			new RegExp(`import ${name} from '\\./${name}\\.svelte';`),
			`if it does not import ${name} then broken`
		);
	}
});

test('side mixer strip reads the shared mixer state, never a copy', async () => {
	const src = await sideMixerSource();
	assert.match(src, /import \{ mixerState \} from '\$lib\/player\/state\.svelte';/);
	assert.match(
		src,
		/mixerState\.channels\[deckId\]/,
		'if it does not read mixerState.channels[deckId] then broken'
	);
	assert.doesNotMatch(
		src,
		/\$state\(/,
		'if it declares $state( then it holds a second copy - broken'
	);
});

test('side prop mirrors one column list', async () => {
	const src = await sideMixerSource();
	assert.match(
		src,
		/export type SideMixerSide = 'outer-left' \| 'outer-right';/,
		'if the side type changed then broken'
	);
	assert.match(src, /const COLUMNS = \['level', 'eq', 'stem'\] as const;/);
	assert.match(
		src,
		/side === 'outer-right'\) return \[\.\.\.COLUMNS\]\.reverse\(\)/,
		'if outer-right is not COLUMNS reversed then broken'
	);
	assert.doesNotMatch(
		src,
		/row-reverse|order:/,
		'if the mirror is CSS order rather than DOM order then broken'
	);
});

test('outer-left SSR emits level column before eq in DOM order', async () => {
	const html = ssr.render(ssr.SideMixerStrip, {
		props: {
			deckId: 2,
			side: 'outer-left',
			mixerStems: true,
			oneq: () => {},
			onfader: () => {},
			onStemGain: () => {}
		}
	}).body;
	const levelAt = html.indexOf('data-column="level"');
	const eqAt = html.indexOf('data-column="eq"');
	assert.ok(levelAt >= 0 && eqAt >= 0, 'if level or eq column is missing from SSR then broken');
	assert.ok(levelAt < eqAt, 'if outer-left does not emit level before eq in DOM then broken');
});
