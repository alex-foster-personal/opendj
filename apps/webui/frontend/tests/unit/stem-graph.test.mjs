import assert from 'node:assert/strict';
import { before, test } from 'node:test';

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
