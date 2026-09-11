import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let health;

before(async () => {
	health = await loadTypeScriptModule('src/lib/rb/audio-health.svelte.ts');
});

test('a visible waveform paint reports raw frame gaps over the explicit 34ms threshold', () => {
	health.noteWaveformPaintFrame(1, 1_000);
	health.noteWaveformPaintFrame(1, 1_016);
	health.noteWaveformPaintFrame(1, 1_052);
	health.noteWaveformPaintFrame(1, 3_053);
	const metric = health.waveformStutterSnapshot();
	assert.equal(metric.active, true);
	assert.equal(metric.window_ms, 2_000);
	assert.equal(metric.elapsed_ms, 2_053);
	assert.equal(metric.threshold_ms, 34);
	assert.equal(metric.stutters, 2);
	assert.equal(metric.worst_gap_ms, 2_001);
	assert.match(health.waveformStutterHover(), /visual canvas cadence, not audio glitches/i);
	health.resetWaveformPaintCadence(1);
});

test('each visible row tracks its own cadence and reset makes stopped rows unavailable', () => {
	health.noteWaveformPaintFrame(2, 1_068);
	health.noteWaveformPaintFrame(2, 1_100);
	assert.equal(health.waveformStutterSnapshot().active, true);
	health.resetWaveformPaintCadence(2);
	assert.equal(health.waveformStutterSnapshot().active, false);
});

test('a shared window flush retains both decks before resetting their next window', () => {
	health.noteWaveformPaintFrame(1, 10_000);
	health.noteWaveformPaintFrame(2, 10_000);
	health.noteWaveformPaintFrame(1, 10_040);
	health.noteWaveformPaintFrame(2, 10_050);
	health.noteWaveformPaintFrame(1, 12_001);
	const metric = health.waveformStutterSnapshot();
	assert.equal(metric.stutters, 3, 'the flushing deck must not erase the other active row');
	assert.equal(metric.frames, 5);
	health.resetWaveformPaintCadence(1);
	health.resetWaveformPaintCadence(2);
	assert.equal(health.waveformStutterSnapshot().active, false);
});
