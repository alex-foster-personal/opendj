/** Pure ring-buffer policy for the cue AudioWorklet bridge (Node-testable). */

export const CUE_BRIDGE_RENDER_QUANTUM = 128;

/** Target fill in frames. ~42.7 ms at 48 kHz and ~46.4 ms at 44.1 kHz: large enough
 * to absorb jitter from two unsynchronized AudioContext render cadences, small enough
 * to stay in the tens-of-ms range the old MediaStream + HTMLAudioElement sink never held. */
export const CUE_BRIDGE_TARGET_FRAMES = 2048;

/** +/- one render quantum around the target: correct with at most one dropped or duplicated
 * frame per callback, inaudible as a click compared with a block-sized jump. */
export const CUE_BRIDGE_DRIFT_BAND_FRAMES = CUE_BRIDGE_RENDER_QUANTUM;

export const CUE_BRIDGE_RING_CAPACITY =
	CUE_BRIDGE_TARGET_FRAMES + CUE_BRIDGE_DRIFT_BAND_FRAMES * 4;

export type CueBridgePullResult = {
	fill: number;
	underrun: boolean;
	dropped: boolean;
	duplicated: boolean;
	priming: boolean;
};

/**
 * Single-threaded ring policy used directly by unit tests and mirrored in the worklet
 * processor. Push and pull are never driven from two agents on this class; the SAB
 * path uses the processor's atomics instead.
 */
export class CueBridgeRing {
	private readonly capacity: number;
	private readonly buffer: Float32Array;
	private writePos = 0;
	private readPos = 0;
	private _fill = 0;
	private _underrunCount = 0;
	private _overflowCount = 0;
	private _priming = true;

	constructor(capacity = CUE_BRIDGE_RING_CAPACITY) {
		if (!Number.isInteger(capacity) || capacity <= CUE_BRIDGE_TARGET_FRAMES) {
			throw new RangeError(`cue bridge ring capacity must exceed target fill, got ${capacity}`);
		}
		this.capacity = capacity;
		this.buffer = new Float32Array(capacity * 2);
	}

	get fill(): number {
		return this._fill;
	}

	get underrunCount(): number {
		return this._underrunCount;
	}

	get overflowCount(): number {
		return this._overflowCount;
	}

	get priming(): boolean {
		return this._priming;
	}

	/** Known bridge buffering latency from the target fill alone (ms). */
	static bufferLatencyMs(sampleRate: number, targetFrames = CUE_BRIDGE_TARGET_FRAMES): number {
		if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
			throw new RangeError(`sampleRate must be finite and positive, got ${sampleRate}`);
		}
		return (targetFrames / sampleRate) * 1000;
	}

	push(left: Float32Array, right: Float32Array): void {
		if (left.length !== right.length) {
			throw new RangeError('cue bridge push requires matching left/right lengths');
		}
		for (let i = 0; i < left.length; i += 1) {
			if (this._fill >= this.capacity) {
				this._overflowCount += 1;
				continue;
			}
			const slot = this.writePos * 2;
			this.buffer[slot] = left[i];
			this.buffer[slot + 1] = right[i];
			this.writePos = (this.writePos + 1) % this.capacity;
			this._fill += 1;
		}
	}

	pull(left: Float32Array, right: Float32Array): CueBridgePullResult {
		if (left.length !== right.length) {
			throw new RangeError('cue bridge pull requires matching left/right lengths');
		}
		if (this._priming) {
			left.fill(0);
			right.fill(0);
			if (this._fill >= CUE_BRIDGE_TARGET_FRAMES) {
				this._priming = false;
			}
			return {
				fill: this._fill,
				underrun: false,
				dropped: false,
				duplicated: false,
				priming: true
			};
		}

		let dropped = false;
		let duplicated = false;
		let underrun = false;
		let outIdx = 0;

		const hi = CUE_BRIDGE_TARGET_FRAMES + CUE_BRIDGE_DRIFT_BAND_FRAMES;
		const lo = CUE_BRIDGE_TARGET_FRAMES - CUE_BRIDGE_DRIFT_BAND_FRAMES;

		if (this._fill > hi) {
			this.readPos = (this.readPos + 1) % this.capacity;
			this._fill -= 1;
			dropped = true;
		} else if (this._fill > 0 && this._fill < lo) {
			// Peek, do not consume: the loop below reads this same frame again into
			// slot 1, so the quantum takes one frame fewer than it outputs and the
			// ring refills by one frame per quantum.
			const slot = this.readPos * 2;
			left[0] = this.buffer[slot];
			right[0] = this.buffer[slot + 1];
			duplicated = true;
			outIdx = 1;
		}

		for (; outIdx < left.length; outIdx += 1) {
			if (this._fill <= 0) {
				left.fill(0, outIdx);
				right.fill(0, outIdx);
				if (!underrun) {
					underrun = true;
					this._underrunCount += 1;
					this._priming = true;
				}
				break;
			}
			const slot = this.readPos * 2;
			left[outIdx] = this.buffer[slot];
			right[outIdx] = this.buffer[slot + 1];
			this.readPos = (this.readPos + 1) % this.capacity;
			this._fill -= 1;
		}

		return { fill: this._fill, underrun, dropped, duplicated, priming: false };
	}
}
