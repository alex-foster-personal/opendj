/**
 * ARTNUM-9649/9649b/10287 (#8404, #10287): the Deck number on artwork pref, its size rule, the
 * rendered overlay and the quality-signal contrast model.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, before, beforeEach, mock, test } from 'node:test';

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
		() => art.validateDeckNumberArtPrefFields({ deck_number_art: 'outline' }, 'k'),
		/off\|halo\|glow\|tinted/
	);
	assert.throws(
		() => art.requireDeckNumberArt('outline', 'mode'),
		/mode must be off\|halo\|glow\|tinted/
	);
	assert.equal(art.parseDeckNumberArt('glow'), 'glow');
	assert.equal(art.parseDeckNumberArt('tinted'), 'tinted');
});

test('[if] halo renders nothing at 40/56/67px or draws at 22px [then] the size rule is broken', () => {
	for (const size of [40, 43, 56, 67]) {
		const body = html({ deck: 2, mode: 'halo', sizePx: size, stableId: null, playing: false });
		assert.match(body, /data-testid="artwork-deck-number"/, `drawn at ${size}px`);
		assert.match(body, />2<\/span>/);
		assert.match(body, new RegExp(`--art-s: ${size}px`));
	}
	assert.doesNotMatch(
		html({ deck: 2, mode: 'halo', sizePx: 22, stableId: null, playing: false }),
		/artwork-deck-number/
	);
	assert.doesNotMatch(
		html({ deck: 2, mode: 'off', sizePx: 67, stableId: null, playing: false }),
		/artwork-deck-number/
	);
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
	assert.ok(art.strokeContrast(0.3, 'none') >= 3);
	assert.ok(art.strokeContrast(0.31, 'none') < 3);
	// Halo modeled at 0.35x: passes up to p90 ~0.857, so the spec's minimum 3.05 corresponds to ~0.84.
	assert.ok(art.strokeContrast(0.84, 'dark-halo') >= 3.05 - 0.01);
	assert.ok(art.strokeContrast(0.86, 'dark-halo') < 3);
	// Negative control: pure white art under the glyph fails even with the halo.
	assert.ok(art.strokeContrast(1, 'dark-halo') < 3);
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
			ipc.dispatchPerformanceCommand({ type: 'set_deck_number_art', mode: 'outline' }),
			/mode must be off\|halo\|glow\|tinted/
		);
		assert.equal(ipc.queryPerformanceState().ui.deck_number_art, 'halo');
		const tinted = await ipc.dispatchPerformanceCommand({
			type: 'set_deck_number_art',
			mode: 'tinted'
		});
		assert.equal(tinted.ui.deck_number_art, 'tinted');
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

// ---------------------------------------------------------------------------
// ARTNUM-11719: glow and tinted.

const RED = { r: 200, g: 30, b: 30 };

function fixturePixels(cells) {
	const out = [];
	for (const [n, rgb] of cells) for (let i = 0; i < n; i++) out.push(rgb[0], rgb[1], rgb[2], 255);
	return out;
}

function fakeCanvas(getImageData) {
	const calls = { draw: 0 };
	const ctx = {
		drawImage: () => {
			calls.draw += 1;
		},
		getImageData
	};
	const factory = () => ({ width: 0, height: 0, getContext: () => ctx });
	return { calls, factory };
}

beforeEach(() => art?.resetDeckNumberArtSampler());
afterEach(() => art?.resetDeckNumberArtSampler());

test('[if] the sampler picks near-black/white or the most frequent muted cell [then] the glow takes the wrong colour', () => {
	const px = fixturePixels([
		[20, [10, 10, 10]],
		[20, [250, 250, 250]],
		[10, [120, 110, 100]],
		[8, [200, 30, 30]],
		[4, [40, 60, 200]],
		[2, [30, 160, 40]]
	]);
	assert.deepEqual(art.dominantColorFromPixels(px), RED);
	assert.equal(
		art.dominantColorFromPixels(
			fixturePixels([
				[32, [0, 0, 0]],
				[32, [255, 255, 255]]
			])
		),
		null
	);
	assert.throws(() => art.dominantColorFromPixels([0, 0, 0, 255]), RangeError);
});

test('[if] dominantColorOf draws again for the same stable_id [then] every mount re-reads the canvas', () => {
	const px = fixturePixels([[64, [200, 30, 30]]]);
	const { calls, factory } = fakeCanvas(() => ({ data: px }));
	art.setDeckNumberArtCanvasFactory(factory);
	const img = {};
	assert.deepEqual(art.dominantColorOf(img, 'sid-1'), RED);
	assert.deepEqual(art.dominantColorOf(img, 'sid-1'), RED);
	assert.equal(calls.draw, 1, 'second mount is a cache hit');
	art.dominantColorOf(img, 'sid-2');
	assert.equal(calls.draw, 2);
	art.dominantColorOf(img, 'sid-2', { refresh: true });
	assert.equal(calls.draw, 3);
});

test('[if] a tainted canvas throws or does not fall back to halo [then] a remote cover breaks the deck header', () => {
	const tainted = fakeCanvas(() => {
		throw new DOMException('tainted', 'SecurityError');
	});
	art.setDeckNumberArtCanvasFactory(tainted.factory);
	assert.equal(art.dominantColorOf({}, 'remote'), null);
	assert.equal(art.effectiveDeckNumberArt('tinted', null), 'halo');
	assert.equal(art.effectiveDeckNumberArt('glow', null), 'halo');
	assert.equal(art.effectiveDeckNumberArt('glow', RED), 'glow');
	art.resetDeckNumberArtSampler();
	const broken = fakeCanvas(() => {
		throw new Error('boom');
	});
	art.setDeckNumberArtCanvasFactory(broken.factory);
	assert.throws(() => art.dominantColorOf({}, 'x'), /boom/);
});

test('[if] glow or tinted drops the stroke under 3:1 on the luminance cases [then] the number is unreadable on bright art', () => {
	for (const mode of ['glow', 'tinted']) {
		const backing = art.strokeBackingOf(mode);
		assert.ok(art.strokeContrast(0.3, backing) >= 3, mode);
		assert.ok(art.strokeContrast(0.84, backing) >= 3, mode);
		assert.ok(art.strokeContrast(1, backing) < 3, `${mode} negative control`);
	}
	assert.equal(art.strokeBackingOf('halo'), 'dark-halo');
	assert.equal(art.strokeBackingOf('tinted'), 'tinted-halo');
});

test('[if] the animated-cover sampler runs while paused or not at 1 Hz while playing [then] it wastes work or goes stale', () => {
	mock.timers.enable({ apis: ['setInterval'] });
	try {
		const spy = mock.fn();
		const r = art.createTintResampler(spy, art.DECK_NUMBER_ART_RESAMPLE_MS);
		r.setPlaying(true);
		mock.timers.tick(3000);
		assert.equal(spy.mock.callCount(), 3);
		r.setPlaying(false);
		mock.timers.tick(3000);
		assert.equal(spy.mock.callCount(), 3);
		r.setPlaying(true);
		r.setPlaying(true);
		mock.timers.tick(1000);
		assert.equal(spy.mock.callCount(), 4, 'no double interval');
		r.stop();
		mock.timers.tick(3000);
		assert.equal(spy.mock.callCount(), 4);
	} finally {
		mock.timers.reset();
	}
	assert.equal(art.isAnimatedCover({ tagName: 'VIDEO', hasAttribute: () => false }), true);
	assert.equal(
		art.isAnimatedCover({ tagName: 'IMG', hasAttribute: (a) => a === 'data-animated-cover' }),
		true
	);
	assert.equal(art.isAnimatedCover({ tagName: 'IMG', hasAttribute: () => false }), false);
});

test('[if] tinted keeps its colour in the grayscale skins [then] Gothic and Light show a hue the art no longer has', () => {
	const neutral = (css) => {
		const [r, g, b] = css.match(/\d+/g);
		return r === g && g === b;
	};
	const gray = art.grayscaleOf(RED);
	assert.ok(gray.r === gray.g && gray.g === gray.b);
	const vars = art.deckNumberArtGlowVars('tinted', RED);
	assert.ok(neutral(vars['--dna-glow-gray']));
	assert.ok(neutral(vars['--dna-halo-gray']));
	assert.ok(!neutral(vars['--dna-glow']));
	assert.equal(art.deckNumberArtGlowVars('glow', RED)['--dna-halo'], 'rgb(0 0 0)');
	const base = { deck: 1, sizePx: 67, stableId: 's', playing: false };
	const body = html({ ...base, mode: 'tinted', tint: RED });
	assert.match(body, /data-mode="tinted"/);
	assert.match(body, /--dna-glow-gray/);
	assert.match(html({ ...base, mode: 'tinted' }), /data-mode="halo"/);
	const src = readFileSync('src/lib/components/rb/ArtworkDeckNumber.svelte', 'utf8');
	assert.match(
		src,
		/\[data-skin='mono-dev'\], \[data-skin='light'\]\)\) \.deck-num-art \{\s*--dna-halo-now: var\(--dna-halo-gray\);\s*--dna-glow-now: var\(--dna-glow-gray\)/
	);
});
