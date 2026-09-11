// Regression: if current render data is reported as though it were an older
// presented frame, then broken (single stamp or both fields equal to
// presentation time when render leads).
// Regression: presentation_context_time_s must not exceed render_context_time_s.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { buildDeckAudioSnapshot, copyDeckAudioSnapshot } = await loadTypeScriptModule(
	'src/lib/rb/deck-audio-snapshot.ts'
);

function _validInput(overrides = {}) {
	return {
		renderContextTimeS: 10.5,
		presentationContextTimeS: 10.4,
		sampleRateHz: 48000,
		fftSize: 2048,
		frequencyDb: [-60, -30, -10],
		timeDomain: [0.1, -0.2, 0.05],
		...overrides
	};
}

test('buildDeckAudioSnapshot names both clock domains explicitly', () => {
	const snapshot = buildDeckAudioSnapshot(_validInput());
	assert.equal(snapshot.render_context_time_s, 10.5);
	assert.equal(snapshot.presentation_context_time_s, 10.4);
	assert.notEqual(snapshot.render_context_time_s, snapshot.presentation_context_time_s);
	assert.ok('render_context_time_s' in snapshot);
	assert.ok('presentation_context_time_s' in snapshot);
	assert.equal('context_time_s' in snapshot, false);
});

test('buildDeckAudioSnapshot accepts equal clocks and rejects presentation ahead of render', () => {
	const equal = buildDeckAudioSnapshot(
		_validInput({ renderContextTimeS: 5, presentationContextTimeS: 5 })
	);
	assert.equal(equal.render_context_time_s, 5);
	assert.equal(equal.presentation_context_time_s, 5);

	assert.throws(
		() =>
			buildDeckAudioSnapshot(
				_validInput({ renderContextTimeS: 4.9, presentationContextTimeS: 5 })
			),
		/presentation_context_time_s.*render_context_time_s/i
	);
});

test('captureDeckAudio stamps render from currentTime and presentation from last_presentation_context_time_s', () => {
	// render_context_time_s is the analyser-sample clock (AudioContext.currentTime).
	// presentation_context_time_s is the listener clock (last trusted output timestamp).
	const engine = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const captureBlock = engine.slice(
		engine.indexOf('captureDeckAudio(deck: DeckId)'),
		engine.indexOf('setTrim(deck: DeckId')
	);
	assert.match(captureBlock, /buildDeckAudioSnapshot\(/);
	assert.match(captureBlock, /renderContextTimeS:\s*_ctx\.currentTime/);
	assert.match(captureBlock, /presentationContextTimeS:\s*presentationContextTime/);
	assert.match(captureBlock, /last_presentation_context_time_s/);
	assert.doesNotMatch(captureBlock, /context_time_s:/);
});

test('performance IPC _captureUnknown copies through copyDeckAudioSnapshot', () => {
	const ipc = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	const captureBlock = ipc.slice(
		ipc.indexOf('function _captureUnknown'),
		ipc.indexOf('export function installPerformanceBrowserIpc')
	);
	assert.match(captureBlock, /copyDeckAudioSnapshot\(/);
	assert.match(captureBlock, /engine\.captureDeckAudio\(_deck\(deck\)\)/);
});

test('copyDeckAudioSnapshot isolates arrays and requires both named clocks', () => {
	const built = buildDeckAudioSnapshot(_validInput());
	const copied = copyDeckAudioSnapshot(built);
	assert.notEqual(copied.frequency_db, built.frequency_db);
	assert.notEqual(copied.time_domain, built.time_domain);
	copied.frequency_db[0] = 999;
	copied.time_domain[0] = 999;
	assert.notEqual(built.frequency_db[0], 999);
	assert.notEqual(built.time_domain[0], 999);

	assert.throws(
		() => copyDeckAudioSnapshot({ context_time_s: 1, sample_rate_hz: 48000, fft_size: 2048 }),
		/render_context_time_s.*presentation_context_time_s/i
	);
	assert.throws(
		() =>
			copyDeckAudioSnapshot({
				render_context_time_s: 1,
				sample_rate_hz: 48000,
				fft_size: 2048,
				frequency_db: [],
				time_domain: []
			}),
		/render_context_time_s.*presentation_context_time_s/i
	);
});
