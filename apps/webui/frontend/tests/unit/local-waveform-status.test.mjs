import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let localWaveformStatus;

before(async () => {
	localWaveformStatus = await loadTypeScriptModule('src/lib/rb/local-waveform-status.ts');
});

function anlzWith(local_waveform) {
	return {
		stable_id: 'abc123',
		points: 0,
		waveform: { kind: 'mono', preview: { length: 0, low: [], mid: [], high: [] }, detail: { length: 0, low: [], mid: [], high: [] } },
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		local_waveform
	};
}

test('null anlzData never yields a failure reason', () => {
	assert.equal(localWaveformStatus.localDecodeFailureReason(null), null);
});

test('no local_waveform key (a rekordbox-mapped track) yields no failure reason', () => {
	const anlz = anlzWith(undefined);
	assert.equal(localWaveformStatus.localDecodeFailureReason(anlz), null);
});

test('a decoded local_waveform yields no failure reason', () => {
	const anlz = anlzWith({ status: 'decoded', reason: null, preview_b64: 'xyz', preview_max: 12 });
	assert.equal(localWaveformStatus.localDecodeFailureReason(anlz), null);
});

test('a permanent not_decoded local_waveform surfaces its reason', () => {
	const anlz = anlzWith({
		status: 'not_decoded',
		reason: 'ffmpeg is not on PATH, so no local waveform can be decoded',
		preview_b64: null,
		preview_max: null,
		retryable: false
	});
	assert.equal(
		localWaveformStatus.localDecodeFailureReason(anlz),
		'ffmpeg is not on PATH, so no local waveform can be decoded'
	);
});

test('a retryable (transient) not_decoded local_waveform does NOT surface as a permanent failure', () => {
	const anlz = anlzWith({
		status: 'not_decoded',
		reason: 'decoder momentarily saturated',
		preview_b64: null,
		preview_max: null,
		retryable: true
	});
	assert.equal(localWaveformStatus.localDecodeFailureReason(anlz), null);
});
