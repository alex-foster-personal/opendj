import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const MODULE = 'src/lib/rb/silence-source-pcm.ts';

let mod = null;
let loadError = null;

before(async () => {
	try {
		mod = await loadTypeScriptModule(MODULE);
	} catch (error) {
		loadError = error;
	}
});

function _mod() {
	assert.equal(loadError, null, String(loadError));
	return mod;
}

function makeBuffer({ sampleRate = 44_100, length, channels = 1, fill = 0 }) {
	const channelData = [];
	for (let ch = 0; ch < channels; ch++) {
		const data = new Float32Array(length);
		if (typeof fill === 'function') fill(data, ch);
		else data.fill(fill);
		channelData.push(data);
	}
	return {
		sampleRate,
		length,
		numberOfChannels: channels,
		getChannelData(ch) {
			return channelData[ch];
		}
	};
}

test('loud window at playhead yields rms above floor', () => {
	const m = _mod();
	const buffer = makeBuffer({ length: 44_100, fill: 0.5 });
	const rms = m.rmsAtPlayhead(buffer, 0, 0.5);
	assert.ok(rms > 0.001);
});

test('zero tail at playhead yields rms below floor', () => {
	const m = _mod();
	const buffer = makeBuffer({ length: 44_100, fill: 0 });
	const rms = m.rmsAtPlayhead(buffer, 0, 0.5);
	assert.ok(rms < 0.001);
});

test('claimed-live deck with silent tail explains silence', () => {
	const m = _mod();
	const buffer = makeBuffer({ length: 44_100, fill: 0 });
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer, position_sec: 0 }
		]),
		true
	);
});

test('claimed-live deck with loud passage does not explain silence', () => {
	const m = _mod();
	const buffer = makeBuffer({ length: 44_100, fill: 0.5 });
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer, position_sec: 0 }
		]),
		false
	);
});

test('claimed-live deck with null buffer does not explain silence', () => {
	const m = _mod();
	assert.equal(
		m.claimedLiveSourceIsSilent([{ claims_live: true, buffer: null, position_sec: 0 }]),
		false
	);
});

test('two decks: one silent source and one loud source while both claimed-live', () => {
	const m = _mod();
	const silent = makeBuffer({ length: 44_100, fill: 0 });
	const loud = makeBuffer({ length: 44_100, fill: 0.5 });
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer: silent, position_sec: 0 },
			{ claims_live: true, buffer: loud, position_sec: 0 }
		]),
		false
	);
});
