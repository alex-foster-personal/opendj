/**
 * PERFMODE-05 (issue #1987): Hz meter quality check vs presentation ticks.
 *
 * Regression: abs(meter_hz - tick_hz) <= 2 over 500ms window must fail
 * out loud, not silently drift. Locked HZ_METER_MAX_ABS_ERROR_HZ=2.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let health;

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

before(async () => {
	health = await loadTypeScriptModule('src/lib/rb/audio-health.svelte.ts');
});

test('hzMeterQualityOk enforces the 2 Hz ceiling', () => {
	assert.equal(health.hzMeterQualityOk(45, 45), true);
	assert.equal(health.hzMeterQualityOk(45, 47), true);
	assert.equal(health.hzMeterQualityOk(45, 48), false);
	assert.equal(health.hzMeterQualityOk(30, 27), false);
});

test('presentationTickHz computes rate from ticks and elapsed ms', () => {
	assert.equal(health.presentationTickHz(23, 500), 46);
	assert.equal(health.hzMeterQualityOk(Math.round(46), 46), true);
});

test('a mismatch fixture returns ok false from audioHealthQuality shape', () => {
	const meterHz = 60;
	const tickHz = 40;
	assert.equal(health.hzMeterQualityOk(meterHz, tickHz), false);
	assert.equal(health.hzMeterAbsError(meterHz, tickHz), 20);
});

test('audio-health sampler still derives _hz from noteAudioPresentationTick ticks', () => {
	const src = source('src/lib/rb/audio-health.svelte.ts');
	assert.match(src, /noteAudioPresentationTick/);
	assert.match(src, /presentationTickHz\(_ticks/);
	assert.doesNotMatch(src, /requestAnimationFrame/);
});
