/**
 * ARTNUM-9649/9649b (#8404): the Deck number on artwork pref, its size rule, the
 * rendered overlay and the quality-signal contrast model.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let art;
let ssr;

before(async () => {
	art = await loadTypeScriptModule('src/lib/rb/deck-number-art.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as ArtworkDeckNumber } from '$lib/components/rb/ArtworkDeckNumber.svelte';",
			"export { default as WaveTrackSummaryView } from '$lib/components/rb/wave/WaveTrackSummaryView.svelte';",
			"export * as stories from '$lib/components/rb/ArtworkDeckNumber.stories';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const html = (props) => ssr.render(ssr.ArtworkDeckNumber, { props }).body;

test('[if] the pref defaults to anything but off [then] existing users see a change they did not ask for', () => {
	assert.deepEqual(art.DECK_NUMBER_ART_PREF_DEFAULTS, { deck_number_art: 'off' });
	assert.deepEqual(art.validateDeckNumberArtPrefFields({}, 'k'), {});
	assert.deepEqual(art.validateDeckNumberArtPrefFields({ deck_number_art: 'halo' }, 'k'), {
		deck_number_art: 'halo'
	});
	assert.throws(
		() => art.validateDeckNumberArtPrefFields({ deck_number_art: 'tinted' }, 'k'),
		/off\|halo/
	);
	assert.throws(() => art.requireDeckNumberArt('glow', 'mode'), /mode must be off\|halo/);
});

test('[if] halo renders nothing at 40/56/67px or draws at 22px [then] the size rule is broken', () => {
	for (const size of [40, 43, 56, 67]) {
		const body = html({ deck: 2, mode: 'halo', sizePx: size });
		assert.match(body, /data-testid="artwork-deck-number"/, `drawn at ${size}px`);
		assert.match(body, />2<\/span>/);
		assert.match(body, new RegExp(`--art-s: ${size}px`));
	}
	assert.doesNotMatch(html({ deck: 2, mode: 'halo', sizePx: 22 }), /artwork-deck-number/);
	assert.doesNotMatch(html({ deck: 2, mode: 'off', sizePx: 67 }), /artwork-deck-number/);
});

test('[if] the gutter view draws the number before the art loads or with the pref off [then] it covers an empty square', () => {
	const props = {
		fullTitle: 'Song',
		artworkSrc: '/a.jpg',
		liveKey: null,
		keyColor: null,
		liveBpm: null,
		onArtworkLoad: () => {},
		onArtworkError: () => {},
		deckNumber: 3,
		deckNumberArt: 'halo'
	};
	// SSR has not loaded the image, so the overlay must be absent.
	assert.doesNotMatch(ssr.render(ssr.WaveTrackSummaryView, { props }).body, /artwork-deck-number/);
	assert.match(ssr.render(ssr.WaveTrackSummaryView, { props }).body, /wave-art/);
});

test('[if] every story does not render [then] the Storybook catalog entry is broken', () => {
	const names = Object.keys(ssr.stories).filter((k) => k !== 'default');
	assert.ok(names.length >= 4);
	for (const name of names) {
		const props = { ...ssr.stories.default.args, ...(ssr.stories[name].args ?? {}) };
		const body = html(props);
		const expectDrawn = props.mode !== 'off' && props.sizePx >= 40;
		assert.equal(/artwork-deck-number/.test(body), expectDrawn, name);
	}
});

test('[if] the halo model passes art the spec says fails, or plain outline passes bright art [then] the quality signal is wrong', () => {
	assert.equal(art.wcagContrast(1, 0), 21);
	assert.equal(art.DECK_NUMBER_ART_MIN_CONTRAST, 3);
	// Plain outline: fails once p90 art luminance exceeds 0.30 (white needs L <= 0.30 for 3:1).
	assert.ok(art.strokeContrast(0.3, false) >= 3);
	assert.ok(art.strokeContrast(0.31, false) < 3);
	// Halo modeled at 0.35x: passes up to p90 ~0.857, so the spec's minimum 3.05 corresponds to ~0.84.
	assert.ok(art.strokeContrast(0.84, true) >= 3.05 - 0.01);
	assert.ok(art.strokeContrast(0.86, true) < 3);
	// Negative control: pure white art under the glyph fails even with the halo.
	assert.ok(art.strokeContrast(1, true) < 3);
	assert.throws(() => art.wcagContrast(1.2, 0), RangeError);
});

test('[if] set_deck_number_art does not show up in the query state ui [then] an agent cannot read back the mode it set', async () => {
	const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		assert.equal(ipc.queryPerformanceState().ui.deck_number_art, 'off');
		const after = await ipc.dispatchPerformanceCommand({
			type: 'set_deck_number_art',
			mode: 'halo'
		});
		assert.equal(after.ui.deck_number_art, 'halo');
		assert.equal(ipc.queryPerformanceState().ui.deck_number_art, 'halo');
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'set_deck_number_art', mode: 'tinted' }),
			/mode must be off\|halo/
		);
		assert.equal(ipc.queryPerformanceState().ui.deck_number_art, 'halo');
	} finally {
		uninstall();
		delete globalThis.window;
	}
});
