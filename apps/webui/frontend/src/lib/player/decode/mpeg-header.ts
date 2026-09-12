/**
 * What MPEG bytes disclose before anything decodes them.
 *
 * Mirror of flac-header.ts: sniff the container and read the sample rate from
 * the first MPEG frame header so an ineligible bundle never spends a worker
 * decode it is going to discard.
 */

const MPEG1_SAMPLE_RATES = [44100, 48000, 32000];
const MPEG2_SAMPLE_RATES = [22050, 24000, 16000];
const MPEG25_SAMPLE_RATES = [11025, 12000, 8000];

/** Skip an ID3v2 tag at byte 0. Returns the offset of the first audio byte. */
function _skipId3v2(bytes: Uint8Array): number {
	if (bytes.length < 10) return 0;
	if (bytes[0] !== 0x49 || bytes[1] !== 0x44 || bytes[2] !== 0x33) return 0;
	const size =
		((bytes[6] & 0x7f) << 21) |
		((bytes[7] & 0x7f) << 14) |
		((bytes[8] & 0x7f) << 7) |
		(bytes[9] & 0x7f);
	const footer = (bytes[5] & 0x10) !== 0 ? 10 : 0;
	return 10 + size + footer;
}

/** True when `offset` points at an MPEG frame sync word. */
function _isFrameSync(bytes: Uint8Array, offset: number): boolean {
	return (
		offset + 1 < bytes.length &&
		bytes[offset] === 0xff &&
		(bytes[offset + 1] & 0xe0) === 0xe0
	);
}

/**
 * Where the first MPEG frame must start: immediately after an ID3 tag, or at 0.
 *
 * Deliberately does NOT scan the whole file. Random 0xFF 0xEx pairs appear deep
 * inside WAV and other containers and would false-positive as MPEG.
 */
function _firstFrameOffset(bytes: Uint8Array): number {
	const start = _skipId3v2(bytes);
	return _isFrameSync(bytes, start) ? start : -1;
}

/**
 * The four magic conditions for an MPEG container: frame sync after any ID3.
 *
 * A short buffer is not a match.
 */
export function isMpegContainer(bytes: ArrayBuffer): boolean {
	if (bytes.byteLength < 4) return false;
	return _firstFrameOffset(new Uint8Array(bytes)) >= 0;
}

/**
 * The stream's sample rate from the first MPEG frame header, or null when the
 * bytes do not say.
 */
export function mpegStreamSampleRate(bytes: ArrayBuffer): number | null {
	if (!isMpegContainer(bytes)) return null;
	const view = new Uint8Array(bytes);
	const offset = _firstFrameOffset(view);
	if (offset < 0 || offset + 3 >= view.length) return null;
	const h = view[offset + 2];
	const versionBits = (view[offset + 1] >> 3) & 0x03;
	const layerBits = (view[offset + 1] >> 1) & 0x03;
	// Layer III only for stem bundles.
	if (layerBits !== 0x01) return null;
	const rateIndex = (h >> 2) & 0x03;
	if (rateIndex === 0x03) return null;
	if (versionBits === 0x03) return MPEG1_SAMPLE_RATES[rateIndex];
	if (versionBits === 0x02) return MPEG2_SAMPLE_RATES[rateIndex];
	if (versionBits === 0x00) return MPEG25_SAMPLE_RATES[rateIndex];
	return null;
}
