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
			{ claims_live: true, buffer, position_sec: 0, master_path_gain: 1 }
		]),
		true
	);
});

test('claimed-live deck with loud passage does not explain silence', () => {
	const m = _mod();
	const buffer = makeBuffer({ length: 44_100, fill: 0.5 });
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer, position_sec: 0, master_path_gain: 1 }
		]),
		false
	);
});

test('claimed-live deck with null buffer does not explain silence', () => {
	const m = _mod();
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer: null, position_sec: 0, master_path_gain: 1 }
		]),
		false
	);
});

test('two decks: one silent source and one loud source while both claimed-live', () => {
	const m = _mod();
	const silent = makeBuffer({ length: 44_100, fill: 0 });
	const loud = makeBuffer({ length: 44_100, fill: 0.5 });
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{
				claims_live: true,
				buffer: silent,
				position_sec: 0,
				master_path_gain: 1
			},
			{ claims_live: true, buffer: loud, position_sec: 0, master_path_gain: 1 }
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
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer, position_sec: 0, master_path_gain: 1 }
		]),
		true
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer, position_sec: 2.4, master_path_gain: 1 }
		]),
		true
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer, position_sec: 2.9, master_path_gain: 1 }
		]),
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
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer, position_sec: 0.5, master_path_gain: 1 }
		]),
		true
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer, position_sec: 1, master_path_gain: 1 }
		]),
		false
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer, position_sec: 5, master_path_gain: 1 }
		]),
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
			m.claimedLiveSourceIsSilent([
				{ claims_live: true, buffer, position_sec, master_path_gain: 1 }
			]),
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
			{
				claims_live: true,
				buffer: silent,
				position_sec: 0,
				master_path_gain: 1
			},
			{
				claims_live: false,
				buffer: loud,
				position_sec: 0,
				master_path_gain: 1
			}
		]),
		true
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{
				claims_live: false,
				buffer: silent,
				position_sec: 0,
				master_path_gain: 1
			}
		]),
		false
	);
});

test('a playing deck with its path to the master closed explains master silence', () => {
	// Fader down, crossfader cut, master at zero, or every stem muted: the deck
	// plays loud source into the headphones and nothing of it reaches the
	// master bus, which is ordinary DJ pre-cueing, not a dropout. A closed
	// path explains silence without its PCM being read, so a missing buffer is
	// no reason to report either.
	const m = _mod();
	const loud = makeBuffer({ length: 44_100, fill: 0.5 });
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer: loud, position_sec: 0, master_path_gain: 0 }
		]),
		true
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer: null, position_sec: 0, master_path_gain: 0 }
		]),
		true
	);
	// Control, the overshoot: the same loud deck with its path open still
	// reports, and so does an absent gain, which never explains silence.
	for (const master_path_gain of [1, 0.5, undefined]) {
		assert.equal(
			m.claimedLiveSourceIsSilent([
				{ claims_live: true, buffer: loud, position_sec: 0, master_path_gain }
			]),
			false,
			`gain ${master_path_gain} must not hide a loud source`
		);
	}
});

test('a closed-path deck does not excuse an open-path deck that is loud', () => {
	const m = _mod();
	const loud = makeBuffer({ length: 44_100, fill: 0.5 });
	const silent = makeBuffer({ length: 44_100, fill: 0 });
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer: loud, position_sec: 0, master_path_gain: 0 },
			{ claims_live: true, buffer: loud, position_sec: 0, master_path_gain: 1 }
		]),
		false
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{ claims_live: true, buffer: loud, position_sec: 0, master_path_gain: 0 },
			{
				claims_live: true,
				buffer: silent,
				position_sec: 0,
				master_path_gain: 1
			}
		]),
		true
	);
});

test('source RMS is scaled by the master path gain and summed across decks', () => {
	// 0.01 RMS through a fader at 0.05 reaches the master at 0.0005, under the
	// 0.001 floor; the same source at 0.5 reaches 0.005, over it.
	const m = _mod();
	const quiet = makeBuffer({ length: 44_100, fill: 0.01 });
	const at = (master_path_gain) => ({
		claims_live: true,
		buffer: quiet,
		position_sec: 0,
		master_path_gain
	});
	assert.equal(m.claimedLiveSourceIsSilent([at(0.05)]), true);
	assert.equal(m.claimedLiveSourceIsSilent([at(0.5)]), false);
	// Two decks each under the floor alone but over it together: the sum bounds
	// the mixed RMS from above, so this is not explained.
	assert.equal(m.claimedLiveSourceIsSilent([at(0.06), at(0.06)]), false);
});

test('a path gain that cannot be read does not explain silence', () => {
	const m = _mod();
	const silent = makeBuffer({ length: 44_100, fill: 0 });
	for (const master_path_gain of [Number.NaN, Number.POSITIVE_INFINITY, -1]) {
		assert.equal(
			m.claimedLiveSourceIsSilent([
				{
					claims_live: true,
					buffer: silent,
					position_sec: 0,
					master_path_gain
				}
			]),
			false,
			`gain ${master_path_gain}`
		);
	}
});

test('an absent path gain does not explain silence, even over silent PCM', () => {
	// Trim can boost above unity, so an unknown gain is not "open at 1": a
	// 0.0008 RMS source through trim at 2 reaches the master over the floor.
	// Control: the same silent deck with a read gain does explain it.
	const m = _mod();
	const silent = makeBuffer({ length: 44_100, fill: 0 });
	assert.equal(
		m.claimedLiveSourceIsSilent([{ claims_live: true, buffer: silent, position_sec: 0 }]),
		false
	);
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{
				claims_live: true,
				buffer: silent,
				position_sec: 0,
				master_path_gain: 1
			}
		]),
		true
	);
});

test('non-finite PCM is an invalid measurement, never silence', () => {
	const m = _mod();
	for (const bad of [Number.NaN, Number.POSITIVE_INFINITY]) {
		const buffer = makeBuffer({
			length: 44_100,
			fill: (data) => (data[10] = bad)
		});
		assert.equal(Number.isFinite(m.rmsAtPlayhead(buffer, 0, 0.5)), false);
		assert.equal(
			m.claimedLiveSourceIsSilent([
				{ claims_live: true, buffer, position_sec: 0, master_path_gain: 1 }
			]),
			false,
			`PCM holding ${bad}`
		);
	}
});

test('a playhead before the decoded start does not explain silence', () => {
	// A negative start index would otherwise read as an empty, silent window
	// and excuse a loud open-path deck for as long as the bad position lasted.
	// Control: the same loud deck at a valid playhead does not explain it either,
	// and a silent deck at a valid playhead does.
	const m = _mod();
	const loud = makeBuffer({ length: 44_100, fill: 0.5 });
	const silent = makeBuffer({ length: 44_100, fill: 0 });
	for (const buffer of [loud, silent]) {
		assert.equal(
			m.claimedLiveSourceIsSilent([
				{ claims_live: true, buffer, position_sec: -0.25, master_path_gain: 1 }
			]),
			false
		);
	}
	assert.equal(Number.isNaN(m.rmsAtPlayhead(loud, -0.25, 0.5)), true);
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{
				claims_live: true,
				buffer: silent,
				position_sec: 0,
				master_path_gain: 1
			}
		]),
		true
	);
});

// ---------------------------------------------------------------- stem parts
// AUDIOLIVE-17. A ready stem bundle's PCM is TRANSFERRED to its worklet
// (PERF-STEMDEC-05), so the gate cannot read a part at the playhead. Each
// part keeps a block-peak envelope taken before the transfer instead, and a
// stem deck's expected contribution is path gain x the sum of part gain x
// part peak. Peak bounds RMS from above, so this can only over-predict sound.

function peakEnv(m, blocks, block_sec = 0.1) {
	// A real envelope built from PCM, one constant level per block.
	const sampleRate = 1_000;
	const per = Math.round(block_sec * sampleRate);
	const buffer = makeBuffer({
		sampleRate,
		length: blocks.length * per,
		fill: (data) => {
			for (let b = 0; b < blocks.length; b++) data.fill(blocks[b], b * per, (b + 1) * per);
		}
	});
	return m.peakEnvelope(buffer, block_sec);
}

test('peakEnvelope keeps the largest absolute sample of each block across every channel', () => {
	const m = _mod();
	const buffer = makeBuffer({
		sampleRate: 1_000,
		length: 250,
		channels: 2,
		fill: (data, ch) => {
			if (ch === 0) data[10] = -0.4; // block 0, negative: magnitude counts
			if (ch === 1) data[150] = 0.7; // block 1, second channel
			if (ch === 0) data[240] = 0.2; // trailing partial block 2
		}
	});
	const env = m.peakEnvelope(buffer, 0.1);
	assert.equal(env.block_sec, 0.1);
	assert.equal(env.duration_sec, 0.25);
	assert.deepEqual(
		Array.from(env.peaks, (v) => Math.round(v * 1000) / 1000),
		[0.4, 0.7, 0.2]
	);
	assert.throws(() => m.peakEnvelope(buffer, 0), RangeError);
});

test('peakAtPlayhead is the max over the blocks the window overlaps, NaN outside the track', () => {
	const m = _mod();
	const env = peakEnv(m, [0, 0, 0.3, 0, 0]);
	assert.equal(m.peakAtPlayhead(env, 0, 0.15), 0, 'blocks 0..1 are silent');
	assert.ok(
		Math.abs(m.peakAtPlayhead(env, 0.05, 0.2) - 0.3) < 1e-6,
		'window reaching block 2 sees it'
	);
	assert.equal(Number.isNaN(m.peakAtPlayhead(env, 0.5, 0.1)), true, 'at the end is outside');
	assert.equal(
		Number.isNaN(m.peakAtPlayhead(env, -0.01, 0.1)),
		true,
		'before the start is outside'
	);
	assert.equal(Number.isNaN(m.peakAtPlayhead(env, Number.NaN, 0.1)), true);
});

test('if soloing a stem that is silent in this passage reads as a dropout then broken', () => {
	// Repro (bifrost1, Sat 26 Sep 2026): VOCAL soloed through an instrumental
	// break. The mix PCM at the playhead is loud, the soloed part is not, and
	// the deck was stopped as AUDIO CUT.
	const m = _mod();
	const loudMix = makeBuffer({ length: 44_100, fill: 0.5 });
	const deck = {
		claims_live: true,
		buffer: loudMix,
		position_sec: 0,
		master_path_gain: 1,
		stem_parts: [
			{ gain: 1, envelope: peakEnv(m, [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]) }, // vocal, soloed, silent here
			{
				gain: 0,
				envelope: peakEnv(m, [0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6])
			} // drums, gated off by solo
		]
	};
	assert.equal(m.claimedLiveSourceIsSilent([deck]), true);
});

test('if soloing a stem that is LOUD in this passage hides a dropout then broken', () => {
	// The overshoot control: solo must not become a blanket excuse.
	const m = _mod();
	const deck = (vocalLevel) => ({
		claims_live: true,
		buffer: null,
		position_sec: 0,
		master_path_gain: 1,
		stem_parts: [
			{ gain: 1, envelope: peakEnv(m, Array(10).fill(vocalLevel)) },
			{ gain: 0, envelope: peakEnv(m, Array(10).fill(0.6)) }
		]
	});
	assert.equal(m.claimedLiveSourceIsSilent([deck(0.4)]), false);
	// Path gain still scales the parts: a loud solo through a closed fader is silent.
	assert.equal(m.claimedLiveSourceIsSilent([{ ...deck(0.4), master_path_gain: 0 }]), true);
	// Part gain scales too: 0.01 peak at part gain 0.05 is under the floor, at 0.5 it is over.
	const quiet = (gain) => ({
		...deck(0.01),
		stem_parts: [{ gain, envelope: peakEnv(m, Array(10).fill(0.01)) }]
	});
	assert.equal(m.claimedLiveSourceIsSilent([quiet(0.05)]), true);
	assert.equal(m.claimedLiveSourceIsSilent([quiet(0.5)]), false);
});

test('if a quiet soloed part with sparse transients reads as expected sound then broken (r4268103313)', () => {
	// A decaying tail: one 0.004 click per 100 ms block, otherwise silent.
	// Window RMS is 0.004 / sqrt(100) = 0.0004, under the 0.001 floor, while
	// every block PEAK is 0.004, 12 dB over it. The master meter reads RMS, so
	// a peak-based expectation called this quiet master an AUDIO CUT.
	const m = _mod();
	const sampleRate = 1_000;
	const clicks = makeBuffer({
		sampleRate,
		length: 1_000,
		fill: (data) => {
			for (let i = 0; i < data.length; i += 100) data[i] = 0.004;
		}
	});
	const env = m.peakEnvelope(clicks, 0.1);
	const rms = m.rmsAtPlayhead(clicks, 0.2);
	assert.ok(rms < 0.001, `window RMS ${rms} is under the floor`);
	assert.ok(m.peakAtPlayhead(env, 0.2) >= 0.001, 'while the block peak is over it');
	const deck = {
		claims_live: true,
		buffer: null,
		position_sec: 0.2,
		master_path_gain: 1,
		stem_parts: [{ gain: 1, envelope: env }]
	};
	assert.equal(m.claimedLiveSourceIsSilent([deck]), true, 'quiet source explains quiet master');
	// Control: the same clicks 4x louder (RMS 0.0016) still mean sound was expected.
	const loud = makeBuffer({
		sampleRate,
		length: 1_000,
		fill: (data) => {
			for (let i = 0; i < data.length; i += 100) data[i] = 0.016;
		}
	});
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{
				...deck,
				stem_parts: [{ gain: 1, envelope: m.peakEnvelope(loud, 0.1) }]
			}
		]),
		false
	);
});

test('rmsBoundAtPlayhead never reads below rmsAtPlayhead and is exact on block-aligned windows', () => {
	const m = _mod();
	const sampleRate = 8_000;
	let seed = 7;
	const noise = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648) * 2 - 1;
	const buffer = makeBuffer({
		sampleRate,
		length: sampleRate * 3 + 123,
		channels: 2,
		fill: (data) => {
			for (let i = 0; i < data.length; i++)
				data[i] = noise() * (0.2 + 0.8 * Math.abs(Math.sin(i / 3000)));
		}
	});
	const env = m.peakEnvelope(buffer, 0.1);
	for (const pos of [0, 0.03, 0.47, 1.111, 2.5, 2.9, 3.01]) {
		const exact = m.rmsAtPlayhead(buffer, pos);
		const bound = m.rmsBoundAtPlayhead(env, pos);
		assert.ok(bound >= exact * (1 - 1e-5), `at ${pos}: bound ${bound} >= rms ${exact}`);
	}
	// Block-aligned full windows read exactly the window: no extra blocks.
	for (const pos of [0, 0.5, 1.2, 2.4]) {
		const exact = m.rmsAtPlayhead(buffer, pos);
		assert.ok(
			Math.abs(m.rmsBoundAtPlayhead(env, pos) - exact) <= exact * 1e-4,
			`aligned at ${pos}`
		);
	}
	assert.equal(Number.isNaN(m.rmsBoundAtPlayhead(env, 3.1)), true, 'past the end is NaN');
});

test('a stem deck whose parts cannot be measured does not explain silence', () => {
	const m = _mod();
	const base = {
		claims_live: true,
		buffer: null,
		position_sec: 0,
		master_path_gain: 1
	};
	const silentEnv = peakEnv(m, Array(10).fill(0));
	// Open part with no envelope.
	assert.equal(
		m.claimedLiveSourceIsSilent([{ ...base, stem_parts: [{ gain: 1, envelope: null }] }]),
		false
	);
	// Unreadable part gain.
	for (const gain of [Number.NaN, -1, Number.POSITIVE_INFINITY]) {
		assert.equal(
			m.claimedLiveSourceIsSilent([{ ...base, stem_parts: [{ gain, envelope: silentEnv }] }]),
			false,
			`part gain ${gain}`
		);
	}
	// No parts at all: a stem graph with nothing in it is not a measurement.
	assert.equal(m.claimedLiveSourceIsSilent([{ ...base, stem_parts: [] }]), false);
	// Playhead past the envelope: a stuck transport, never content.
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{
				...base,
				position_sec: 5,
				stem_parts: [{ gain: 1, envelope: silentEnv }]
			}
		]),
		false
	);
	// Control: a closed part needs no envelope, and the silent open part explains.
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{
				...base,
				stem_parts: [
					{ gain: 0, envelope: null },
					{ gain: 1, envelope: silentEnv }
				]
			}
		]),
		true
	);
});

test('if non-finite stem PCM reads as a silent part then broken (an invalid sample poisons its block)', () => {
	// Found by mutation: an envelope that skips NaN with `mag > peak` turns a
	// corrupt part into a silent one, and the gate excuses a real dropout.
	const m = _mod();
	const corrupt = makeBuffer({
		sampleRate: 1_000,
		length: 1_000,
		fill: (data) => {
			data[150] = Number.NaN;
		}
	});
	const env = m.peakEnvelope(corrupt, 0.1);
	assert.equal(Number.isNaN(env.peaks[1]), true, 'the NaN block is NaN, not 0');
	assert.equal(env.peaks[0], 0, 'and its neighbours are untouched');
	assert.equal(
		m.claimedLiveSourceIsSilent([
			{
				claims_live: true,
				buffer: null,
				position_sec: 0.1,
				master_path_gain: 1,
				stem_parts: [{ gain: 1, envelope: env }]
			}
		]),
		false
	);
});
