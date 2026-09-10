/**
 * What FLAC bytes say about themselves before anything decodes them.
 *
 * The stem decode rung has to answer two questions per part BEFORE it spends a
 * worker on it: is this FLAC at all, and is it at a rate this context can use.
 * Answering the second one after the decode is what makes the decode wasted -
 * a 44.1kHz bundle in a 48kHz context decodes in the worker, is refused, and
 * decodes again through `decodeAudioData`, twice per part, on every load. The
 * header answers it for free.
 *
 * Deliberately its own module, not a corner of the policy file: this is format
 * knowledge with a spec behind it, and it is the half of the decision that can
 * be checked against the format rather than against the rung's intent.
 */

const FLAC_MAGIC = [0x66, 0x4c, 0x61, 0x43];

/** The four magic bytes, read defensively: a short buffer is not a match. */
export function isFlacContainer(bytes: ArrayBuffer): boolean {
	if (bytes.byteLength < FLAC_MAGIC.length) return false;
	const head = new Uint8Array(bytes, 0, FLAC_MAGIC.length);
	return FLAC_MAGIC.every((byte, i) => head[i] === byte);
}

/**
 * The stream's sample rate from STREAMINFO, or null when the bytes do not say.
 *
 * Null is a real answer and never a synonym for "no": a stream this cannot
 * read must fall through to the decoder's own verdict rather than be refused
 * on a guess, so every caller has to handle it as UNKNOWN. Zero is the
 * format's own way of saying unknown, so it comes back as null too.
 *
 * Layout, from the FLAC specification: `fLaC`, then a four-byte metadata block
 * header whose low seven bits are the block type, then the block. STREAMINFO
 * is type 0 and the spec requires it FIRST, so any other type here is a stream
 * this function does not speak for. Inside STREAMINFO, at byte 8: two block
 * sizes (4 bytes), two frame sizes (6 bytes), then the sample rate as 20 bits
 * starting at byte 18, which is why it needs a nibble out of byte 20.
 */
export function flacStreamSampleRate(bytes: ArrayBuffer): number | null {
	if (!isFlacContainer(bytes) || bytes.byteLength < 21) return null;
	const head = new Uint8Array(bytes, 0, 21);
	if ((head[4] & 0x7f) !== 0) return null;
	const rate = (head[18] << 12) | (head[19] << 4) | (head[20] >> 4);
	return rate === 0 ? null : rate;
}
