/**
 * Virtual-edit tail trim: coordinates stay in source time (LIBEDIT-9179).
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let truncateAtOutPoint;

class FakeBuffer {
	#channels = [];
	constructor(numberOfChannels, length, sampleRate) {
		this.numberOfChannels = numberOfChannels;
		this.length = length;
		this.sampleRate = sampleRate;
		this.duration = length / sampleRate;
	}
	getChannelData(channel) {
		if (this.#channels[channel] === undefined) {
			this.#channels[channel] = new Float32Array(this.length);
		}
		return this.#channels[channel];
	}
}

/** Minimal Web Audio stand-in: node:test has no AudioContext. */
class FakeAudioContext {
	constructor({ sampleRate = 48_000 } = {}) {
		this.sampleRate = sampleRate;
	}
	createBuffer(channels, length, rate) {
		return new FakeBuffer(channels, length, rate);
	}
	close() {}
}

before(async () => {
	({ truncateAtOutPoint } = await loadTypeScriptModule('src/lib/rb/edit-window-buffer.ts'));
});

describe('truncateAtOutPoint', () => {
	it('returns the same buffer when out_ms is absent or past the decode', () => {
		const ctx = new FakeAudioContext({ sampleRate: 48_000 });
		const buf = ctx.createBuffer(1, 48_000, 48_000);
		assert.equal(truncateAtOutPoint(ctx, buf, null), buf);
		assert.equal(truncateAtOutPoint(ctx, buf, buf.duration * 1000), buf);
	});

	it('truncates decoded audio at the edit out point in source time', () => {
		const ctx = new FakeAudioContext({ sampleRate: 48_000 });
		const buf = ctx.createBuffer(1, 96_000, 48_000);
		const ch = buf.getChannelData(0);
		ch.fill(0.5);
		const out = truncateAtOutPoint(ctx, buf, 500);
		assert.equal(out.duration, 0.5);
		assert.equal(out.length, 24_000);
		assert.equal(out.getChannelData(0)[23_999], 0.5);
	});
});
