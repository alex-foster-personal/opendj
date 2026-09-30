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

test('silent intro explains silence at the start and stops explaining where the music begins', () => {
	// AC4 shape: 3 s of digital silence, then a loud body. The playhead window
	// is silent through the intro, so the fold is never fed a countable
	// sample there; once the window reaches the body it no longer explains.
	const m = _mod();
	const sampleRate = 44_100;
	const buffer = makeBuffer({
		sampleRate,
		length: sampleRate * 10,
		fill: (data) => data.fill(0.5, sampleRate * 3)
	});
	assert.equal(
		m.claimedLiveSourceIsSilent([{ claims_live: true, buffer, position_sec: 0 }]),
		true
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([{ claims_live: true, buffer, position_sec: 2.4 }]),
		true
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([{ claims_live: true, buffer, position_sec: 2.9 }]),
		false,
		'a window that reaches the loud body must not explain master silence'
	);
});

test('a playhead at or past the decoded end does not explain silence', () => {
	// Fail-safe: a deck still claiming live past its end is a stuck transport,
	// which the watchdog's honest stop exists to catch. Control: the same
	// silent buffer one window before the end still explains silence, so the
	// silent-outro fix (AC1) is not undone by this guard.
	const m = _mod();
	const buffer = makeBuffer({ length: 44_100, fill: 0 });
	assert.equal(
		m.claimedLiveSourceIsSilent([{ claims_live: true, buffer, position_sec: 0.5 }]),
		true
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([{ claims_live: true, buffer, position_sec: 1 }]),
		false
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([{ claims_live: true, buffer, position_sec: 5 }]),
		false
	);
});

test('a non-finite playhead does not explain silence and does not throw', () => {
	// A throw here would abort noteMasterSilence before its fold, so the
	// watchdog would stop counting for as long as the bad position lasted.
	const m = _mod();
	const buffer = makeBuffer({ length: 44_100, fill: 0 });
	for (const position_sec of [Number.NaN, Number.POSITIVE_INFINITY]) {
		assert.equal(
			m.claimedLiveSourceIsSilent([{ claims_live: true, buffer, position_sec }]),
			false
		);
	}
});

test('decks that do not claim live are ignored', () => {
	const m = _mod();
	const silent = makeBuffer({ length: 44_100, fill: 0 });
	const loud = makeBuffer({ length: 44_100, fill: 0.5 });
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer: silent, position_sec: 0 },
			{ claims_live: false, buffer: loud, position_sec: 0 }
		]),
		true
	);
	assert.equal(m.claimedLiveSourceIsSilent([{ claims_live: false, buffer: silent, position_sec: 0 }]), false);
});
