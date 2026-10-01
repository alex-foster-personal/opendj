/**
 * How decoded PCM reaches a stretch worklet (PERF-STEMDEC-05).
 *
 * The worklet needs each channel as a Float32Array whose ArrayBuffer is in
 * the postMessage transfer list. Until round 3 every load first COPIED each
 * channel out of the AudioBuffer on the main thread. For a stem bundle that
 * is eight channels, about 250 MB allocated and written in about 80 ms, and
 * with another deck playing that burst alone made the audio callback late:
 * 12 late callbacks over 30 bundles against 0.35 expected from the idle rate
 * measured between them (ops/perf/stem-decode-under-playback-round-3). The
 * four WebAssembly instantiations it was first blamed on measured 0 over 30.
 *
 * A stem AudioBuffer has no reader after its processor is loaded, so its
 * channels can be MOVED: the transfer detaches the AudioBuffer's own storage
 * and the worklet owns it, with no allocation and no copy. The mix buffer is
 * read again (waveform, reloads), so it keeps the copy.
 *
 * Requirements (mini-PRD):
 *   ✔︎ A stem load moves its channels when the engine can move them.
 *     [if] transfer is requested and the probe passed [then] the posted
 *       arrays ARE the AudioBuffer's own channel views and nothing is copied
 *   ✔︎ A copy load never touches the source.
 *     [if] copy is requested [then] the source channels stay attached and
 *       the posted arrays are fresh
 *   ✔︎ An engine that cannot move a channel falls back to the copy, visibly.
 *     [if] the probe fails or a view is not a whole private buffer [then]
 *       the load copies and reports 'copy', never a partial move
 */

export type PcmHandoff = 'copy' | 'transfer';

/** The slice of AudioBuffer this module reads. */
export interface PcmSource {
	readonly numberOfChannels: number;
	readonly length: number;
	getChannelData(channel: number): Float32Array;
	copyFromChannel(destination: Float32Array, channel: number): void;
}

/**
 * True when moving every view's buffer moves exactly the channels and nothing
 * else: each view spans a whole, non-shared ArrayBuffer of `frames` samples,
 * and no two views share one.
 */
export function channelsAreTransferable(views: readonly Float32Array[], frames: number): boolean {
	const seen = new Set<ArrayBufferLike>();
	for (const view of views) {
		if (!(view.buffer instanceof ArrayBuffer)) return false;
		if (view.length !== frames || view.byteOffset !== 0) return false;
		if (view.byteLength !== view.buffer.byteLength) return false;
		if (seen.has(view.buffer)) return false;
		seen.add(view.buffer);
	}
	return views.length > 0;
}

/**
 * Does this engine move an AudioBuffer channel when its buffer is transferred?
 * Proven on a throwaway buffer, because a refused transfer on the real load
 * would fail the load. `move` is null where structuredClone does not exist.
 */
export function probeChannelTransfer(
	makeBuffer: () => PcmSource,
	move: ((view: Float32Array) => Float32Array) | null
): boolean {
	if (move === null) return false;
	const buffer = makeBuffer();
	const view = buffer.getChannelData(0);
	const marker = 0.25;
	view[view.length - 1] = marker;
	if (!channelsAreTransferable([view], buffer.length)) return false;
	let moved: Float32Array;
	try {
		moved = move(view);
	} catch (error) {
		// The one refusal this probe exists to find: the engine will not detach
		// the channel. Anything else is a defect and must surface.
		if (error instanceof DOMException && error.name === 'DataCloneError') return false;
		throw error;
	}
	return moved.length === buffer.length && moved[moved.length - 1] === marker && view.length === 0;
}

/**
 * The channel arrays to post, each with its buffer in the transfer list.
 * `canTransfer` is the probe's verdict; a 'transfer' request it cannot honor,
 * or whose views are not whole private buffers, is served as a copy and says so.
 */
export function pcmChannelsForWorklet(
	buffer: PcmSource,
	requested: PcmHandoff,
	canTransfer: boolean
): { channels: Float32Array[]; handoff: PcmHandoff } {
	if (requested !== 'copy' && requested !== 'transfer') {
		throw new RangeError(`unknown PCM handoff ${String(requested)}`);
	}
	const indexes = Array.from({ length: buffer.numberOfChannels }, (_unused, channel) => channel);
	if (requested === 'transfer' && canTransfer) {
		const views = indexes.map((channel) => buffer.getChannelData(channel));
		if (channelsAreTransferable(views, buffer.length)) return { channels: views, handoff: 'transfer' };
	}
	const channels = indexes.map((channel) => {
		const samples = new Float32Array(buffer.length);
		buffer.copyFromChannel(samples, channel);
		return samples;
	});
	return { channels, handoff: 'copy' };
}
