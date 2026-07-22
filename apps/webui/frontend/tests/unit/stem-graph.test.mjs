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

test('stem controls and part maps reject incomplete keys instead of defaulting', () => {
	assert.throws(
		() => stems.stemPartGains({ vocal: { muted: false, solo: false } }),
		/stem controls/i
	);
	assert.throws(
		() => stems.validateStemBufferAlignment({ vocals: ALIGNED.vocals }),
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
