import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * What FLAC bytes disclose before anything decodes them.
 *
 * This exists because the rate has to be known BEFORE a worker is spent: a
 * bundle at a rate the context does not run decodes in the worker, is refused,
 * and decodes again natively, on every load, forever.
 *
 * Regression lines:
 * - if the rate is read from the wrong bytes then an eligible bundle is barred
 *   from the worker lane and the rung silently buys nothing
 * - if an unreadable header returns a number then a stream this module cannot
 *   parse is refused on a guess
 * - if the STREAMINFO block type is not checked then a stream whose first
 *   metadata block is something else is read as a rate
 */

let header;

before(async () => {
	header = await loadTypeScriptModule('src/lib/player/decode/flac-header.ts');
});

/** A STREAMINFO header carrying `rate`, built to the spec's bit layout. */
function flacHeader(rate, { blockType = 0, length = 64 } = {}) {
	const out = new Uint8Array(length);
	out.set([0x66, 0x4c, 0x61, 0x43], 0);
	out[4] = blockType;
	out[5] = 0;
	out[6] = 0;
	out[7] = 34;
	// 20 bits of sample rate starting at byte 18, so byte 20's high nibble is
	// the last four bits and its low nibble belongs to the channel count.
	out[18] = (rate >> 12) & 0xff;
	out[19] = (rate >> 4) & 0xff;
	out[20] = ((rate << 4) & 0xf0) | 0x01;
	return out.buffer;
}

//-----------------------------------------------------------------------------

test('the container is sniffed from the bytes, not assumed', () => {
	assert.equal(header.isFlacContainer(flacHeader(44100)), true);
	assert.equal(header.isFlacContainer(new Uint8Array([0x4f, 0x67, 0x67, 0x53]).buffer), false);
	assert.equal(header.isFlacContainer(new Uint8Array([0x66, 0x4c]).buffer), false);
	assert.equal(header.isFlacContainer(new ArrayBuffer(0)), false);
});

test('the sample rate comes out of STREAMINFO, at every rate that ships', () => {
	// The POSITIVE half, and it has to be exact: a reader that returned a
	// plausible constant would satisfy every "is it null" case below.
	for (const rate of [44100, 48000, 88200, 96000, 22050, 8000, 192000]) {
		assert.equal(header.flacStreamSampleRate(flacHeader(rate)), rate, `rate ${rate}`);
	}
	// The 20-bit field's extremes, since the last four bits share a byte with
	// the channel count and an off-by-one nibble would still look sensible.
	assert.equal(header.flacStreamSampleRate(flacHeader(1)), 1);
	assert.equal(header.flacStreamSampleRate(flacHeader(0xfffff)), 0xfffff);
});

test('bytes that do not say report null, never a guessed number', () => {
	// Each of these must be UNKNOWN rather than a rate, because a wrong number
	// here bars an eligible bundle from the worker lane permanently.
	assert.equal(header.flacStreamSampleRate(new Uint8Array([0x4f, 0x67, 0x67, 0x53]).buffer), null);
	assert.equal(header.flacStreamSampleRate(flacHeader(44100, { length: 20 })), null, 'truncated');
	assert.equal(
		header.flacStreamSampleRate(flacHeader(44100, { blockType: 4 })),
		null,
		'a first metadata block that is not STREAMINFO says nothing about the rate'
	);
	// The last-block flag is the high bit and is not part of the type.
	assert.equal(header.flacStreamSampleRate(flacHeader(48000, { blockType: 0x80 })), 48000);
	// Zero is the format's own way of saying unknown, not 0 Hz.
	assert.equal(header.flacStreamSampleRate(flacHeader(0)), null);
});
