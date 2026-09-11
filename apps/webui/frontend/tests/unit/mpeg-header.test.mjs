import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let header;

before(async () => {
	header = await loadTypeScriptModule('src/lib/player/decode/mpeg-header.ts');
});

/** Build a minimal MPEG-1 Layer III frame header at `rate`. */
function mpeg1Frame(rate, { length = 64 } = {}) {
	const rates = [44100, 48000, 32000];
	const idx = rates.indexOf(rate);
	assert.ok(idx >= 0, `unsupported MPEG-1 rate ${rate}`);
	const out = new Uint8Array(length);
	out[0] = 0xff;
	out[1] = 0xfb;
	out[2] = (idx << 2) & 0x0c;
	return out.buffer;
}

function mpeg2Frame(rate, { length = 64 } = {}) {
	const rates = [22050, 24000, 16000];
	const idx = rates.indexOf(rate);
	assert.ok(idx >= 0, `unsupported MPEG-2 rate ${rate}`);
	const out = new Uint8Array(length);
	out[0] = 0xff;
	out[1] = 0xf3;
	out[2] = (idx << 2) & 0x0c;
	return out.buffer;
}

function mpeg25Frame(rate, { length = 64 } = {}) {
	const rates = [11025, 12000, 8000];
	const idx = rates.indexOf(rate);
	assert.ok(idx >= 0, `unsupported MPEG-2.5 rate ${rate}`);
	const out = new Uint8Array(length);
	out[0] = 0xff;
	out[1] = 0xe3;
	out[2] = (idx << 2) & 0x0c;
	return out.buffer;
}

function id3Prefixed(frame) {
	const tag = new Uint8Array(10 + frame.byteLength);
	tag.set([0x49, 0x44, 0x33, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00], 0);
	tag.set(new Uint8Array(frame), 10);
	return tag.buffer;
}

//-----------------------------------------------------------------------------

test('the container is sniffed from frame sync, not assumed', () => {
	assert.equal(header.isMpegContainer(mpeg1Frame(44100)), true);
	assert.equal(header.isMpegContainer(new Uint8Array([0x66, 0x4c, 0x61, 0x43]).buffer), false);
	assert.equal(header.isMpegContainer(new Uint8Array([0xff]).buffer), false);
	assert.equal(header.isMpegContainer(new ArrayBuffer(0)), false);
});

test('ID3-prefixed frames are still MPEG', () => {
	assert.equal(header.isMpegContainer(id3Prefixed(mpeg1Frame(44100))), true);
});

test('the sample rate comes from the MPEG header at every rate that ships', () => {
	for (const rate of [44100, 48000, 32000]) {
		assert.equal(header.mpegStreamSampleRate(mpeg1Frame(rate)), rate, `MPEG-1 ${rate}`);
	}
	for (const rate of [22050, 24000, 16000]) {
		assert.equal(header.mpegStreamSampleRate(mpeg2Frame(rate)), rate, `MPEG-2 ${rate}`);
	}
	for (const rate of [11025, 12000, 8000]) {
		assert.equal(header.mpegStreamSampleRate(mpeg25Frame(rate)), rate, `MPEG-2.5 ${rate}`);
	}
});

test('bytes that do not say report null, never a guessed number', () => {
	assert.equal(header.mpegStreamSampleRate(new Uint8Array([0x4f, 0x67, 0x67, 0x53]).buffer), null);
	assert.equal(header.mpegStreamSampleRate(mpeg1Frame(44100, { length: 3 })), null, 'truncated');
	const reserved = mpeg1Frame(44100);
	const view = new Uint8Array(reserved);
	view[2] = (view[2] & ~0x0c) | 0x0c;
	assert.equal(header.mpegStreamSampleRate(reserved), null, 'reserved rate index');
});
