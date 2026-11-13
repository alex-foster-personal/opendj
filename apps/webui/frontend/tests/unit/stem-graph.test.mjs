import assert from 'node:assert/strict';
import { before, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let stems;

before(async () => {
	stems = await loadTypeScriptModule('src/lib/rb/stem-graph.ts');
});

const ALIGNED = {
	vocals: { sampleRate: 48_000, length: 960_000, numberOfChannels: 2, duration: 20 },
	drums: { sampleRate: 48_000, length: 960_000, numberOfChannels: 2, duration: 20 },
	bass: { sampleRate: 48_000, length: 960_000, numberOfChannels: 2, duration: 20 },
	other: { sampleRate: 48_000, length: 960_000, numberOfChannels: 2, duration: 20 }
};

test('four real Demucs parts must be sample-aligned before graph publication', () => {
	assert.deepEqual(stems.validateStemBufferAlignment(ALIGNED), {
		sample_rate_hz: 48_000,
		frame_count: 960_000,
		channel_count: 2,
		duration_ms: 20_000
	});

	for (const [field, value] of [
		['sampleRate', 44_100],
		['length', 959_999],
		['numberOfChannels', 1]
	]) {
		assert.throws(
			() =>
				stems.validateStemBufferAlignment({
					...ALIGNED,
					other: { ...ALIGNED.other, [field]: value }
				}),
			/alignment/i
		);
	}
});

test('instrumental control owns bass and other without synthesising subtraction', () => {
	const controls = stems.createDefaultStemControls();
	controls.instrumental.muted = true;
	assert.deepEqual(stems.stemPartGains(controls), {
		vocals: 1,
		drums: 1,
		bass: 0,
		other: 0
	});
});

test('independent bass and harmonics retain grouped instrumental gain and mute', () => {
	const controls = stems.createDefaultStemControls();
	assert.ok(controls.bass, 'the real bass branch needs its own control');
	assert.ok(controls.other, 'the real other branch needs its own control');
	controls.bass.muted = true;
	assert.deepEqual(stems.stemPartGains(controls), { vocals: 1, drums: 1, bass: 0, other: 1 });
	controls.bass.muted = false;
	controls.bass.gain = 0.25;
	controls.instrumental.gain = 0.25;
	assert.deepEqual(stems.stemPartGains(controls), { vocals: 1, drums: 1, bass: 0.25, other: 0.5 });
	controls.instrumental.muted = true;
	assert.deepEqual(stems.stemPartGains(controls), { vocals: 1, drums: 1, bass: 0, other: 0 });
});

test('child solos select their real branch, and unavailable solos cannot mute RoFormer', () => {
	const controls = stems.createDefaultStemControls();
	assert.ok(controls.bass);
	controls.bass.solo = true;
	assert.deepEqual(stems.stemPartGains(controls), { vocals: 0, drums: 0, bass: 1, other: 0 });
	assert.deepEqual(stems.stemPartGains(controls, 'roformer2'), { vocals: 1, instrumental: 1 });
	controls.instrumental.solo = true;
	assert.deepEqual(stems.stemPartGains(controls), { vocals: 0, drums: 0, bass: 1, other: 1 });
	controls.other.muted = true;
	assert.deepEqual(stems.stemPartGains(controls), { vocals: 0, drums: 0, bass: 1, other: 0 });
});

test('solo matrix gates every non-solo group and explicit mute wins over solo', () => {
	const controls = stems.createDefaultStemControls();
	controls.vocal.solo = true;
	assert.deepEqual(stems.stemPartGains(controls), {
		vocals: 1,
		drums: 0,
		bass: 0,
		other: 0
	});

	controls.vocal.muted = true;
	assert.deepEqual(stems.stemPartGains(controls), {
		vocals: 0,
		drums: 0,
		bass: 0,
		other: 0
	});
});

test('stem gain defaults to unity and mute still wins', () => {
	const controls = stems.createDefaultStemControls();
	assert.deepEqual(stems.stemPartGains(controls), {
		vocals: 1,
		drums: 1,
		bass: 1,
		other: 1
	});
	controls.vocal.gain = 0;
	assert.deepEqual(stems.stemPartGains(controls).vocals, 0);
	controls.vocal.gain = 0.5;
	controls.vocal.muted = true;
	assert.deepEqual(stems.stemPartGains(controls).vocals, 0);
});

test('roformer2 instrumental gain drives the instrumental part', () => {
	const controls = stems.createDefaultStemControls();
	controls.instrumental.gain = 0.25;
	const gains = stems.stemPartGains(controls, 'roformer2');
	assert.equal(gains.instrumental, 0.5);
});

test('stem controls and part maps reject incomplete keys instead of defaulting', () => {
	assert.throws(
		() => stems.stemPartGains({ vocal: { muted: false, solo: false, gain: 0.5 } }),
		/stem controls/i
	);
	assert.throws(
		() => stems.validateStemBufferAlignment({ vocals: ALIGNED.vocals }),
		/match no known layout/i
	);
});

const ROFORMER_ALIGNED = {
	vocals: { sampleRate: 48_000, length: 960_000, numberOfChannels: 2, duration: 20 },
	instrumental: { sampleRate: 48_000, length: 960_000, numberOfChannels: 2, duration: 20 }
};

test('a two-part RoFormer buffer set is recognised as its own layout', () => {
	assert.equal(stems.layoutOfBuffers(ALIGNED), 'demucs4');
	assert.equal(stems.layoutOfBuffers(ROFORMER_ALIGNED), 'roformer2');
	assert.deepEqual(stems.validateStemBufferAlignment(ROFORMER_ALIGNED), {
		sample_rate_hz: 48_000,
		frame_count: 960_000,
		channel_count: 2,
		duration_ms: 20_000
	});
});

test('solo VOCAL on a roformer2 deck plays vocals alone', () => {
	const controls = stems.createDefaultStemControls();
	controls.vocal.solo = true;
	assert.deepEqual(stems.stemPartGains(controls, 'roformer2'), {
		vocals: 1,
		instrumental: 0
	});
});

test('a control the layout cannot drive can never silence the deck', () => {
	// DRUMS is not a roformer2 control: its parts live inside `instrumental`,
	// so soloing it must not gate the two real branches to zero.
	const controls = stems.createDefaultStemControls();
	controls.drums.solo = true;
	assert.deepEqual(stems.stemPartGains(controls, 'roformer2'), {
		vocals: 1,
		instrumental: 1
	});

	controls.drums.solo = false;
	controls.drums.muted = true;
	assert.deepEqual(stems.stemPartGains(controls, 'roformer2'), {
		vocals: 1,
		instrumental: 1
	});
});

test('each layout advertises only the controls it can genuinely drive', () => {
	assert.deepEqual(stems.readyStemDeckState(
		{ source: 'roformer', model: 'mel-band-roformer', layout: 'roformer2' },
		{ sample_rate_hz: 48_000, frame_count: 960_000, channel_count: 2, duration_ms: 20_000 }
	).available_controls, ['vocal', 'instrumental']);

	assert.deepEqual(stems.readyStemDeckState(
		{ source: 'demucs', model: 'htdemucs', layout: 'demucs4' },
		{ sample_rate_hz: 48_000, frame_count: 960_000, channel_count: 2, duration_ms: 20_000 }
	).available_controls, ['vocal', 'instrumental', 'drums', 'bass', 'other']);
});

test('a roformer2 schedule reaches both branches and no phantom third', async () => {
	const calls = [];
	const processors = Object.fromEntries(
		stems.ROFORMER_PARTS.map((part) => [
			part,
			{ schedule: async (outputTime, change) => calls.push({ part, outputTime, change }) }
		])
	);
	const change = { active: true, input: 3.25, rate: 1.02, semitones: 0 };
	await stems.scheduleAlignedStemProcessors(processors, 10.5, change, 'roformer2');
	assert.deepEqual(
		calls,
		stems.ROFORMER_PARTS.map((part) => ({ part, outputTime: 10.5, change }))
	);

	// A four-part processor map is not a valid roformer2 graph.
	await assert.rejects(
		stems.scheduleAlignedStemProcessors(
			Object.fromEntries(stems.DEMUCS_PARTS.map((part) => [part, { schedule: async () => {} }])),
			10.5,
			change,
			'roformer2'
		),
		/exactly/i
	);
});

test('every real branch receives the identical transport schedule', async () => {
	const calls = [];
	const processors = Object.fromEntries(
		stems.DEMUCS_PARTS.map((part) => [
			part,
			{
				schedule: async (outputTime, change) => calls.push({ part, outputTime, change })
			}
		])
	);
	const change = { active: true, input: 3.25, rate: 1.02, semitones: 0 };
	await stems.scheduleAlignedStemProcessors(processors, 10.5, change);
	assert.deepEqual(
		calls,
		stems.DEMUCS_PARTS.map((part) => ({ part, outputTime: 10.5, change }))
	);
});

test('a partial stem schedule failure rejects the aligned graph transaction', async () => {
	const processors = Object.fromEntries(
		stems.DEMUCS_PARTS.map((part) => [
			part,
			{
				schedule: async () => {
					if (part === 'bass') throw new Error('bass processor failed');
				}
			}
		])
	);
	await assert.rejects(
		stems.scheduleAlignedStemProcessors(processors, 10.5, { active: true, input: 0 }),
		/aligned stem schedule failed on 1 branch/i
	);
});

// STEM-8302: real decoded lengths from the maintainer's library (Tue 3 Nov 2026). LAME
// parts at 96 kbps carry one packet past their Xing frame count, which
// CoreAudio decodes as TRAILING frames; a 48 kHz source resampled to 44.1 kHz
// rounds one frame long. All of it is end-of-file slack, never an offset.
// Channel data is allocated lazily (the real lengths are up to 26M frames), and
// a ramp part reads (i % 1000 + 1) / 1000 so trim/pad positions are checkable.
class FakeBuffer {
	#channels = [];
	constructor(numberOfChannels, length, sampleRate, ramp = false) {
		this.numberOfChannels = numberOfChannels;
		this.length = length;
		this.sampleRate = sampleRate;
		this.duration = length / sampleRate;
		this.ramp = ramp;
	}
	getChannelData(channel) {
		if (this.#channels[channel] === undefined) {
			const data = new Float32Array(this.length);
			if (this.ramp) for (let i = 0; i < this.length; i++) data[i] = ((i % 1000) + 1) / 1000;
			this.#channels[channel] = data;
		}
		return this.#channels[channel];
	}
	copyToChannel(source, channel) {
		this.getChannelData(channel).set(source);
	}
}

const fakeCtx = { createBuffer: (channels, length, rate) => new FakeBuffer(channels, length, rate) };
const rampAt = (i) => Math.fround(((i % 1000) + 1) / 1000);

function rampPart(length, rate = 44_100) {
	return new FakeBuffer(1, length, rate, true);
}

function demucsParts(lengths) {
	return Object.fromEntries(Object.entries(lengths).map(([part, length]) => [part, rampPart(length)]));
}

test('STEM-8302: a drums part one MP3 packet long (+1152) is trimmed at its end and aligns', () => {
	const source = 26_457_088;
	const buffers = demucsParts({ vocals: source, drums: source + 1152, bass: source, other: source });
	assert.throws(() => stems.validateStemBufferAlignment(buffers), /stem alignment mismatch for drums/);
	const conformed = stems.conformStemBuffersToFrameCount(fakeCtx, buffers, source, 44_100);
	assert.equal(stems.validateStemBufferAlignment(conformed).frame_count, source);
	const drums = conformed.drums.getChannelData(0);
	// Start untouched: sample 0 and the last kept sample are the original's, unshifted.
	assert.equal(drums[0], rampAt(0));
	assert.equal(drums[source - 1], rampAt(source - 1));
	assert.equal(conformed.vocals, buffers.vocals, 'an exact part is passed through, not copied');
});

test('STEM-8302: a drums part +704 frames long (decoded tail past the gapless end) is trimmed', () => {
	const source = 11_751_424;
	const buffers = demucsParts({ vocals: source, drums: source + 704, bass: source + 704, other: source });
	const conformed = stems.conformStemBuffersToFrameCount(fakeCtx, buffers, source, 44_100);
	assert.deepEqual(
		Object.values(conformed).map((buffer) => buffer.length),
		[source, source, source, source]
	);
	assert.equal(conformed.bass.getChannelData(0)[1234], rampAt(1234));
	assert.equal(conformed.bass.getChannelData(0)[source - 1], rampAt(source - 1));
});

test('STEM-8302: stems one frame SHORTER than a resampled source are padded with trailing silence', () => {
	const source = 7_532_986;
	const buffers = demucsParts({ vocals: source - 1, drums: source - 1, bass: source - 1, other: source - 1 });
	const conformed = stems.conformStemBuffersToFrameCount(fakeCtx, buffers, source, 44_100);
	for (const buffer of Object.values(conformed)) {
		assert.equal(buffer.length, source);
		assert.equal(buffer.getChannelData(0)[source - 1], 0, 'the padded frame is silence');
		assert.equal(buffer.getChannelData(0)[source - 2], rampAt(source - 2), 'the last real frame is kept, not shifted');
	}
	assert.equal(stems.validateStemBufferAlignment(conformed).frame_count, source);
});

test('STEM-8302: a mismatch past two MP3 frames is NOT conformed and still refuses', () => {
	const source = 11_751_424;
	const tooFar = source + stems.STEM_LENGTH_TOLERANCE_FRAMES + 1;
	assert.equal(stems.STEM_LENGTH_TOLERANCE_FRAMES, 2 * 1152);
	const buffers = demucsParts({ vocals: source, drums: tooFar, bass: source, other: source });
	const conformed = stems.conformStemBuffersToFrameCount(fakeCtx, buffers, source, 44_100);
	assert.equal(conformed.drums, buffers.drums);
	assert.throws(() => stems.validateStemBufferAlignment(conformed), /stem alignment mismatch for drums/);
	// A whole bundle seconds short of its source stays short, for the stem/source check to refuse.
	const wrongEdit = demucsParts({ vocals: source - 44_100, drums: source - 44_100, bass: source - 44_100, other: source - 44_100 });
	const kept = stems.conformStemBuffersToFrameCount(fakeCtx, wrongEdit, source, 44_100);
	assert.equal(stems.validateStemBufferAlignment(kept).frame_count, source - 44_100);
});

test('STEM-8302: a part at another sample rate is left for the alignment check to refuse', () => {
	const source = 960_000;
	const buffers = { ...demucsParts({ vocals: source, bass: source, other: source }), drums: rampPart(source + 1, 48_000) };
	const conformed = stems.conformStemBuffersToFrameCount(fakeCtx, buffers, source, 44_100);
	assert.equal(conformed.drums, buffers.drums);
	assert.throws(() => stems.validateStemBufferAlignment(conformed), /alignment mismatch/);
	assert.throws(() => stems.conformStemBuffersToFrameCount(fakeCtx, buffers, 0, 44_100), RangeError);
});

// STEMPERF-11663: one FX send per part, at 0, in parallel with the part gain.
function fxFakeNode(value = 1) {
	return {
		gain: { value, calls: [], setTargetAtTime(v, t, c) { this.calls.push([v, t, c]); } },
		connections: [],
		disconnects: 0,
		connect(node) { this.connections.push(node); },
		disconnect() { this.disconnects += 1; }
	};
}

function fxProcessor() {
	const ctx = { currentTime: 0, createGain: () => fxFakeNode() };
	const procs = {};
	const gains = {};
	const sends = {};
	for (const part of ['vocals', 'drums', 'bass', 'other']) {
		procs[part] = { connections: [], connect(n) { this.connections.push(n); }, disconnect() {}, dispose: async () => {} };
		const bus = stems.createStemPartBus(ctx, procs[part]);
		gains[part] = bus.gain;
		sends[part] = bus.send;
	}
	return { p: new stems.AlignedStemDeckProcessor(ctx, procs, gains, sends, 0, 'demucs4', {}), procs, gains, sends };
}

test('createStemPartBus feeds a unity gain and a zero send from the source', () => {
	const { procs, gains, sends } = fxProcessor();
	assert.equal(gains.vocals.gain.value, 1);
	assert.equal(sends.vocals.gain.value, 0);
	assert.deepEqual(procs.vocals.connections, [gains.vocals, sends.vocals]);
});

test('a 4-part processor exposes one send per part, connectFxSend and teardown', async () => {
	const { p, gains, sends } = fxProcessor();
	assert.deepEqual(Object.keys(p.fxSends).sort(), ['bass', 'drums', 'other', 'vocals']);
	for (const send of Object.values(p.fxSends)) assert.equal(send.gain.value, 0);
	const dest = { name: 'fxIn' };
	p.connectFxSend(dest);
	for (const send of Object.values(sends)) assert.deepEqual(send.connections, [dest]);
	p.disconnect();
	for (const node of [...Object.values(sends), ...Object.values(gains)]) assert.equal(node.disconnects, 1);
	await p.dispose();
	for (const send of Object.values(sends)) assert.equal(send.disconnects, 2);
});

test('default controls carry fx_send 0 and the send mapping follows the layout', () => {
	const controls = stems.createDefaultStemControls();
	for (const state of Object.values(controls)) assert.equal(state.fx_send, 0);
	controls.instrumental.fx_send = 0.4;
	controls.bass.fx_send = 0.6;
	assert.deepEqual(stems.stemPartSends(controls, 'demucs4'), { vocals: 0, drums: 0, bass: 0.6, other: 0.4 });
	assert.deepEqual(stems.stemPartSends(controls, 'roformer2'), { vocals: 0, instrumental: 0.4 });
});

test('controls validate with and without fx_send and refuse an out-of-range send', () => {
	const legacy = stems.createDefaultStemControls();
	for (const state of Object.values(legacy)) delete state.fx_send;
	assert.doesNotThrow(() => stems.stemPartGains(legacy));
	const bad = stems.createDefaultStemControls();
	bad.vocal.fx_send = 1.5;
	assert.throws(() => stems.stemPartGains(bad));
});

// PERF-STEMDEC (#9988): the four branches are built concurrently.
function createFakes({ delayMs = 50, failCreateAt = -1, latencies = [0.01, 0.01, 0.01, 0.01], handoff = 'transfer' } = {}) {
	const created = [];
	const gainsMade = [];
	const ctx = { currentTime: 0, createGain: () => { const g = fxFakeNode(); gainsMade.push(g); return g; } };
	let calls = 0;
	const wait = () => new Promise((r) => setTimeout(r, delayMs));
	const createProcessor = async () => {
		const index = calls++;
		await wait();
		if (index === failCreateAt) throw new Error('boom');
		const proc = {
			disposed: 0,
			connect() {}, disconnect() {},
			load: async () => { await wait(); return typeof handoff === 'function' ? handoff(index) : handoff; },
			latencySec: async () => latencies[index],
			dispose: async () => { proc.disposed += 1; }
		};
		created.push(proc);
		return proc;
	};
	return { ctx, created, gainsMade, deps: { createProcessor, scanEnvelope: async () => ({ peaks: [] }) } };
}

async function runWithFakeTimers(promise) {
	let settled = false;
	let elapsed = 0;
	const tracked = promise.then((v) => { settled = true; return v; }, (e) => { settled = true; throw e; });
	tracked.catch(() => {});
	while (!settled && elapsed < 2000) {
		for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
		if (settled) break;
		mock.timers.tick(10);
		elapsed += 10;
	}
	return { elapsed, result: tracked };
}

test('[if] each fake create and load takes 50 ms [then] a 4-part create completes under 150 ms, [else stop].', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const fakes = createFakes();
		const { elapsed, result } = await runWithFakeTimers(
			stems.AlignedStemDeckProcessor.create(fakes.ctx, { ...ALIGNED }, {}, fakes.deps)
		);
		await result;
		assert.ok(elapsed < 150, `elapsed ${elapsed} ms`);
	} finally {
		mock.timers.reset();
	}
});

test('[if] parts build concurrently [then] processor part order and transfer verdict are unchanged, [else stop].', async () => {
	const fakes = createFakes({ delayMs: 1 });
	const out = await stems.AlignedStemDeckProcessor.create(fakes.ctx, { ...ALIGNED }, {}, fakes.deps);
	assert.equal(out.pcmHandoff, 'transfer');
	assert.deepEqual(Object.keys(out.processor.fxSends), ['vocals', 'drums', 'bass', 'other']);
	const mixed = createFakes({ delayMs: 1, handoff: (i) => (i === 2 ? 'copy' : 'transfer') });
	const out2 = await stems.AlignedStemDeckProcessor.create(mixed.ctx, { ...ALIGNED }, {}, mixed.deps);
	assert.equal(out2.pcmHandoff, 'copy');
});

test('[if] the third part create rejects [then] every created processor is disposed and gain disconnected, [else stop].', async () => {
	const fakes = createFakes({ delayMs: 1, failCreateAt: 2 });
	await assert.rejects(
		stems.AlignedStemDeckProcessor.create(fakes.ctx, { ...ALIGNED }, {}, fakes.deps),
		/part bass/
	);
	assert.equal(fakes.created.length, 3);
	for (const proc of fakes.created) assert.equal(proc.disposed, 1);
	assert.equal(fakes.gainsMade.length, 6);
	for (const gain of fakes.gainsMade) assert.ok(gain.disconnects >= 1);
});

test('[if] one branch reports another latency [then] create throws the latency mismatch, [else stop].', async () => {
	const fakes = createFakes({ delayMs: 1, latencies: [0.01, 0.01, 0.02, 0.01] });
	await assert.rejects(
		stems.AlignedStemDeckProcessor.create(fakes.ctx, { ...ALIGNED }, {}, fakes.deps),
		/latency alignment mismatch/
	);
	for (const proc of fakes.created) assert.equal(proc.disposed, 1);
});
