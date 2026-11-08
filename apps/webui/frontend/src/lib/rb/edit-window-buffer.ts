/**
 * Truncate decoded PCM at a virtual edit out point (source timeline).
 * Cutting only the tail preserves seek/grid/cue coordinates in source time.
 */
export function truncateAtOutPoint(
	ctx: BaseAudioContext,
	buffer: AudioBuffer,
	outMs: number | null
): AudioBuffer {
	if (outMs === null || !Number.isFinite(outMs) || outMs <= 0) return buffer;
	const durMs = buffer.duration * 1000;
	if (!Number.isFinite(durMs) || outMs >= durMs - 1e-9) return buffer;
	const sr = buffer.sampleRate;
	const frames = Math.max(1, Math.round((outMs * sr) / 1000));
	const ch = buffer.numberOfChannels;
	const out = ctx.createBuffer(ch, frames, sr);
	for (let c = 0; c < ch; c++) {
		const src = buffer.getChannelData(c);
		const dst = out.getChannelData(c);
		dst.set(src.subarray(0, frames));
	}
	return out;
}
