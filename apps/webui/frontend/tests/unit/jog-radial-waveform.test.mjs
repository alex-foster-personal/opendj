// requirement: DECKUX-02
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const jogDialSource = readFileSync(`${SRC}/lib/components/rb/deck/JogDial.svelte`, 'utf8');
const renderSource = readFileSync(`${SRC}/lib/components/rb/wave/render.ts`, 'utf8');

let radial;

test('amp shaping constants still match wave/render.ts', async () => {
	radial = await loadTypeScriptModule('src/lib/components/rb/deck/jog-radial-render.ts');
	const gamma = renderSource.match(/const AMP_GAMMA = ([0-9.]+)/);
	const high = renderSource.match(/const HIGH_BAND_SCALE = ([0-9.]+)/);
	const mid = renderSource.match(/const MID_BAND_SCALE = ([0-9.]+)/);
	const floor = renderSource.match(/const NORM_FLOOR = ([0-9.]+)/);
	assert.ok(gamma && high && mid && floor, 'render.ts must declare the four amp constants');
	assert.equal(radial.JOG_RADIAL_AMP_GAMMA, Number(gamma[1]));
	assert.equal(radial.JOG_RADIAL_HIGH_BAND_SCALE, Number(high[1]));
	assert.equal(radial.JOG_RADIAL_MID_BAND_SCALE, Number(mid[1]));
	assert.equal(radial.JOG_RADIAL_NORM_FLOOR, Number(floor[1]));
});

test('polar geometry maps track start to 12 o-clock and radius scales amplitude', async () => {
	radial = await loadTypeScriptModule('src/lib/components/rb/deck/jog-radial-render.ts');
	assert.equal(radial.polarAngleRad(0, 400), -Math.PI / 2);
	assert.equal(radial.polarAngleRad(400, 400), -Math.PI / 2 + 2 * Math.PI);
	assert.equal(radial.polarRadius(0, 18, 39), 18);
	assert.equal(radial.polarRadius(1, 18, 39), 39);
	assert.equal(radial.JOG_RADIAL_INNER_RADIUS, 18);
	assert.equal(radial.JOG_RADIAL_OUTER_RADIUS, 39);
});

test('vocalArcAngles maps seconds to the same angle convention as preview samples', async () => {
	radial = await loadTypeScriptModule('src/lib/components/rb/deck/jog-radial-render.ts');
	const { startRad, endRad } = radial.vocalArcAngles({ start_s: 0, end_s: 30, intensity: 0.5 }, 120);
	assert.equal(startRad, -Math.PI / 2);
	assert.ok(endRad > startRad);
	assert.ok(Math.abs(endRad - startRad - Math.PI / 2) < 1e-9, '30s of 120s is a quarter turn');
});

test('jogRadialCacheKey never includes position_ms', async () => {
	radial = await loadTypeScriptModule('src/lib/components/rb/deck/jog-radial-render.ts');
	const preview = { length: 2, low: [0.1, 0.2], mid: [0.1, 0.2], high: [0.1, 0.2] };
	const frame = {
		widthPx: 104,
		heightPx: 104,
		playingFace: false,
		kind: 'tri',
		preview,
		vocals: null,
		durationSec: 120
	};
	const key = radial.jogRadialCacheKey(frame, preview, null, 1);
	assert.doesNotMatch(key, /position/i);
	assert.doesNotMatch(jogDialSource, /\$effect\([\s\S]*position_ms[\s\S]*blitJogRadial/);
});

test('playing-face high band stays off the audible wheel fill luminance', async () => {
	radial = await loadTypeScriptModule('src/lib/components/rb/deck/jog-radial-render.ts');
	function lum(hex) {
		const n = parseInt(hex.slice(1), 16);
		const r = ((n >> 16) & 255) / 255;
		const g = ((n >> 8) & 255) / 255;
		const b = (n & 255) / 255;
		return 0.2126 * r + 0.7152 * g + 0.0722 * b;
	}
	const face = lum(radial.JOG_RADIAL_PLAYING_FACE);
	const high = lum('#3a4048');
	assert.ok(Math.abs(face - high) >= 0.2, 'playing high band must not vanish on the off-white face');
});

test('position tick is gated when radial preview is showing', () => {
	assert.match(
		jogDialSource,
		/\{#if !\(radialOn && hasPreview\)\}[\s\S]{0,400}class="position-tick"/,
		'the red tick must hide when radial preview bands are painted'
	);
	assert.match(jogDialSource, /blitJogRadial/);
	assert.match(jogDialSource, /uiPrefs\.jog_radial_waveform/);
});

test('vocal arcs paint only for rekordbox and demucs statuses in the painter', async () => {
	radial = await loadTypeScriptModule('src/lib/components/rb/deck/jog-radial-render.ts');
	const painter = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/deck/jog-radial-render.ts', import.meta.url)),
		'utf8'
	);
	assert.match(painter, /vocals\.status !== 'rekordbox' && vocals\.status !== 'demucs'/);
	assert.doesNotMatch(painter, /not_analyzed[\s\S]{0,80}regions/);
});

test('radial mode draws a red playhead across the bands at the progress angle', () => {
	assert.match(
		jogDialSource,
		/\{:else\}[\s\S]{0,400}class="radial-playhead"[\s\S]{0,400}stroke="#d0342c"[\s\S]{0,200}rotate\(\$\{tickAngle\} 50 50\)/,
		'with the radial waveform showing, the deck still needs a red line where playback is up to'
	);
});

test('waveform bands start near the hub and the text disc is a translucent scrim', async () => {
	radial = await loadTypeScriptModule('src/lib/components/rb/deck/jog-radial-render.ts');
	assert.ok(
		radial.JOG_RADIAL_WAVE_INNER_RADIUS < radial.JOG_RADIAL_INNER_RADIUS,
		'bands must run under the text scrim, not stop at its edge'
	);
	const painter = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/deck/jog-radial-render.ts', import.meta.url)),
		'utf8'
	);
	assert.match(painter, /const rInner = JOG_RADIAL_WAVE_INNER_RADIUS;/);
	const scrim = jogDialSource.match(/\.dial-wrap\.dial-radial-wave \.wheel-fill-inner \{([^}]*)\}/);
	assert.ok(scrim, 'radial inner disc rule present');
	const opacity = Number(/fill-opacity:\s*([0-9.]+)/.exec(scrim[1])?.[1]);
	assert.ok(opacity > 0 && opacity < 1, 'the inner disc must be translucent, never an opaque black cover');
});
