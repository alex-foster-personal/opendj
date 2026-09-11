import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { MPEGDecoderWebWorker } from 'mpg123-decoder';

import { loadTypeScriptModule } from './load-typescript.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../../../../..');
const FIXTURE_DIR = path.join(REPO, 'tests/fixtures/phase7-dedup');
const FIXTURE_RATE = 22050;
const PARTS = ['vocals', 'drums', 'bass', 'other'];

const PINNED_FIXTURES = [
	{
		path: path.join(FIXTURE_DIR, 'src-320.mp3'),
		bytes: 61694,
		sha256: '3614b47af8c2d7ae2696f97b7f3e9658fbf26f68ed7f6854c5909b77d62c44b0'
	},
	{
		path: path.join(FIXTURE_DIR, 'src-128.mp3'),
		bytes: 49363,
		sha256: '922d6cfa0886ef5a6ae195af01d992782d2680bc40244940254934c9aee7c4d0'
	},
	{
		path: path.join(FIXTURE_DIR, 'src-v2.mp3'),
		bytes: 61734,
		sha256: 'bc7c41b952684864814f94bde97528e9364b21c16b4f2c270b53d5ca6cb29fce'
	},
	{
		path: path.join(FIXTURE_DIR, 'other-silent-intro.mp3'),
		bytes: 73083,
		sha256: 'db8d8671be0f6375714e8fefde362ec9d8e43bcc3c725c82a97ba305e9baf45a'
	}
];

let decode;
let mp3Bytes;
const made = [];

function realDecoderFactory() {
	const decoder = new MPEGDecoderWebWorker({ enableGapless: true });
	made.push(decoder);
	return {
		ready: decoder.ready,
		decodeFile: (bytes) => decoder.decode(bytes),
		reset: () => decoder.reset(),
		free: () => decoder.free()
	};
}

function nodeAudioSink(sampleRate) {
	return {
		sampleRate,
		created: [],
		createBuffer(channels, frames, rate) {
			const buffer = {
				numberOfChannels: channels,
				length: frames,
				sampleRate: rate,
				channels: Array.from({ length: channels }, () => null),
				copyToChannel(source, channel) {
					assert.ok(source.length <= frames);
					buffer.channels[channel] = source;
				}
			};
			this.created.push(buffer);
			return buffer;
		}
	};
}

function countingFallback() {
	const calls = [];
	return {
		calls,
		fn: async (bytes) => {
			calls.push(bytes.byteLength);
			return { fallback: true, numberOfChannels: 2, length: 1, sampleRate: FIXTURE_RATE };
		}
	};
}

function bundleOf(bytes) {
	return Object.fromEntries(PARTS.map((part) => [part, bytes.slice().buffer]));
}

before(async () => {
	for (const fixture of PINNED_FIXTURES) {
		assert.ok(existsSync(fixture.path), `UNAVAILABLE: ${fixture.path}`);
		const bytes = await readFile(fixture.path);
		assert.equal(bytes.length, fixture.bytes);
		assert.equal(createHash('sha256').update(bytes).digest('hex'), fixture.sha256);
	}
	mp3Bytes = new Uint8Array(await readFile(PINNED_FIXTURES[0].path));
	decode = await loadTypeScriptModule('src/lib/player/decode/flac-stem-decode.ts');
});

beforeEach(() => {
	decode.setMpegRungShipped(true);
	decode.stemDecodeSession.resetPool();
	decode.stemDecodeSession.resetLane();
	decode.stemDecodeSession.forceLane('workers');
});

after(async () => {
	decode.setMpegRungShipped(false);
	decode.stemDecodeSession.resetPool();
	await Promise.all(made.map((decoder) => decoder.free()));
	made.length = 0;
});

//-----------------------------------------------------------------------------

test('the real MPEG decoder decodes four real mp3 parts through the production path', async () => {
	const ctx = nodeAudioSink(FIXTURE_RATE);
	const fallback = countingFallback();
	const result = await decode.decodeStemParts(ctx, bundleOf(mp3Bytes), PARTS, {
		makeDecoder: realDecoderFactory,
		decodeFallback: fallback.fn
	});
	assert.deepEqual(fallback.calls, []);
	assert.ok(result.reports.every((r) => r.viaWorker && r.refusal === null));
	assert.equal(decode.stemDecodeLabels(result.reports).stem_decode_codec, 'mpeg');
	for (const part of PARTS) {
		const buffer = result.buffers[part];
		assert.equal(buffer.sampleRate, FIXTURE_RATE);
		assert.equal(buffer.length, 66150);
	}
});

test('a real 22.05kHz mp3 is REFUSED by a 44.1kHz context', async () => {
	const ctx = nodeAudioSink(44100);
	const fallback = countingFallback();
	const result = await decode.decodeStemParts(ctx, bundleOf(mp3Bytes), PARTS, {
		makeDecoder: realDecoderFactory,
		decodeFallback: fallback.fn
	});
	assert.equal(fallback.calls.length, PARTS.length);
	assert.ok(result.reports.every((r) => r.refusal === 'sample-rate-mismatch'));
	assert.equal(ctx.created.length, 0);
});

test('every pinned mp3 fixture decodes without errors', async () => {
	for (const fixture of PINNED_FIXTURES) {
		const bytes = new Uint8Array(await readFile(fixture.path));
		const decoder = new MPEGDecoderWebWorker({ enableGapless: true });
		made.push(decoder);
		await decoder.ready;
		const result = await decoder.decode(bytes);
		assert.equal(result.errors?.length ?? 0, 0, fixture.path);
		assert.ok(result.samplesDecoded > 0, fixture.path);
		await decoder.free();
	}
});
