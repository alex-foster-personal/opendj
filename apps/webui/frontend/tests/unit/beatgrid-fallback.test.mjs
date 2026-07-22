import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let beatgridFallback;
let waveMath;

before(async () => {
	beatgridFallback = await loadTypeScriptModule('src/lib/rb/beatgrid-fallback.ts');
	waveMath = await loadTypeScriptModule('src/lib/components/rb/wave/wave-math.ts');
});

test('shouldUseBeatgridFallback only fires once /anlz reports ANALYSIS_NOT_FOUND', () => {
	assert.equal(beatgridFallback.shouldUseBeatgridFallback('ANALYSIS_NOT_FOUND'), true);
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(null), false);
	assert.equal(beatgridFallback.shouldUseBeatgridFallback('SOME_OTHER_ERROR'), false);
	assert.equal(beatgridFallback.shouldUseBeatgridFallback('STATE_DB_UNAVAILABLE'), false);
});

test('toSyntheticAnlzData carries the real beatgrid through, invents nothing else', () => {
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 127.5,
		bpm_confidence: 0.8,
		anlz_available: false,
		beatgrid: {
			beat_count: 2,
			beats: [
				{ n: 1, bpm: 127.5, t: 0.5 },
				{ n: 2, bpm: 127.5, t: 0.971 }
			]
		}
	};

	const synth = beatgridFallback.toSyntheticAnlzData(fallback);

	assert.equal(synth.stable_id, 'abc123');
	assert.deepEqual(synth.beatgrid, fallback.beatgrid);
	assert.deepEqual(synth.cues, []);
	assert.deepEqual(synth.phrases, []);
	assert.deepEqual(synth.vocals, { status: 'not_analyzed' });
	assert.equal(synth.waveform.kind, 'mono');
	assert.equal(synth.waveform.detail.length, 0);
	assert.deepEqual(synth.waveform.detail.low, []);
	assert.deepEqual(synth.waveform.preview.low, []);
});

test('a synthetic payload drives the same bars-to-grid-end countdown as a real one', () => {
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 120,
		bpm_confidence: 0.7,
		anlz_available: false,
		beatgrid: {
			beat_count: 8,
			beats: [
				{ n: 1, bpm: 120, t: 0 },
				{ n: 2, bpm: 120, t: 0.5 },
				{ n: 3, bpm: 120, t: 1.0 },
				{ n: 4, bpm: 120, t: 1.5 },
				{ n: 1, bpm: 120, t: 2.0 },
				{ n: 2, bpm: 120, t: 2.5 },
				{ n: 3, bpm: 120, t: 3.0 },
				{ n: 4, bpm: 120, t: 3.5 }
			]
		}
	};
	const synth = beatgridFallback.toSyntheticAnlzData(fallback);

	// No cues/phrases in the fallback payload, so the countdown target is
	// the end of the measured grid (wave-math.ts priority tier 3): 7 beats
	// from t=0 to the last beat at t=3.5 -> 1 full bar + 3 beats.
	assert.equal(waveMath.barsToNextCueLabel(synth, 0), '1.3Bars');
	// Past the end of the grid: nothing left to count down to.
	assert.equal(waveMath.barsToNextCueLabel(synth, 4000), null);
});

test('no beats at all (no downbeats measured) never invents a grid', () => {
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 0,
		bpm_confidence: 0,
		anlz_available: false,
		beatgrid: { beat_count: 0, beats: [] }
	};
	const synth = beatgridFallback.toSyntheticAnlzData(fallback);
	assert.equal(waveMath.barsToNextCueLabel(synth, 0), null);
});
