/**
 * Cue monitor bridge: sender (main AudioContext) and receiver (cue AudioContext).
 *
 * Plain JS for AudioWorkletGlobalScope. The pull/drift policy matches
 * `$lib/player/cue-bridge-ring.ts`; `tests/unit/cueout-19-bridge-processor-parity.test.mjs`
 * runs the same scenario table against both.
 */

/* global AudioWorkletProcessor, registerProcessor, Atomics, Int32Array, Float32Array */

const RENDER_QUANTUM = 128;
const TARGET_FRAMES = 2048;
const DRIFT_BAND = RENDER_QUANTUM;
const HI = TARGET_FRAMES + DRIFT_BAND;
const LO = TARGET_FRAMES - DRIFT_BAND;

const SAB_WRITE = 0;
const SAB_READ = 1;
const SAB_UNDERRUN = 2;
const SAB_OVERFLOW = 3;
const SAB_HEADER_WORDS = 4;
const SAB_HEADER_BYTES = SAB_HEADER_WORDS * 4;

function sabFill(control, capacity) {
	const write = Atomics.load(control, SAB_WRITE);
	const read = Atomics.load(control, SAB_READ);
	return write >= read ? write - read : capacity - read + write;
}

function interleaveStereo(left, right, out) {
	const frames = left.length;
	if (out.length < frames * 2) {
		throw new RangeError('interleave buffer too small');
	}
	for (let i = 0; i < frames; i += 1) {
		out[i * 2] = left[i];
		out[i * 2 + 1] = right[i];
	}
	return out.subarray(0, frames * 2);
}

function sabPush(control, ring, capacity, left, right) {
	// Fill is derived from write/read alone (no separate atomic counter, so the
	// sender never has to touch SAB_READ to know how full the ring is). That
	// derivation cannot tell "empty" (write === read) apart from "completely
	// full" (write has wrapped all the way back onto read): both compute to 0.
	// One slot is kept permanently unused (capacity - 1, not capacity) so write
	// can never actually reach read except at true empty, the standard
	// single-producer/single-consumer ring-buffer fix for that ambiguity.
	for (let i = 0; i < left.length; i += 1) {
		const fill = sabFill(control, capacity);
		if (fill >= capacity - 1) {
			Atomics.add(control, SAB_OVERFLOW, 1);
			continue;
		}
		const write = Atomics.load(control, SAB_WRITE);
		const slot = write * 2;
		ring[slot] = left[i];
		ring[slot + 1] = right[i];
		Atomics.store(control, SAB_WRITE, (write + 1) % capacity);
	}
}

function pullPolicy(fill, priming, readFrame, ring, capacity, left, right) {
	if (priming) {
		left.fill(0);
		right.fill(0);
		const stillPriming = fill < TARGET_FRAMES;
		return {
			fill,
			dropped: false,
			duplicated: false,
			underrun: false,
			priming: stillPriming,
			nextPriming: stillPriming,
			readFrame
		};
	}

	let dropped = false;
	let duplicated = false;
	let underrun = false;
	let outIdx = 0;
	let localFill = fill;
	let localRead = readFrame;

	if (localFill > HI) {
		localRead = (localRead + 1) % capacity;
		localFill -= 1;
		dropped = true;
	} else if (localFill > 0 && localFill < LO) {
		// Peek, do not consume: the loop below reads this same frame again into
		// slot 1, so the quantum takes one frame fewer than it outputs.
		const slot = localRead * 2;
		left[0] = ring[slot];
		right[0] = ring[slot + 1];
		duplicated = true;
		outIdx = 1;
	}

	for (; outIdx < left.length; outIdx += 1) {
		if (localFill <= 0) {
			left.fill(0, outIdx);
			right.fill(0, outIdx);
			if (!underrun) underrun = true;
			return {
				fill: 0,
				dropped,
				duplicated,
				underrun,
				priming: false,
				nextPriming: true,
				readFrame: localRead
			};
		}
		const slot = localRead * 2;
		left[outIdx] = ring[slot];
		right[outIdx] = ring[slot + 1];
		localRead = (localRead + 1) % capacity;
		localFill -= 1;
	}

	return {
		fill: localFill,
		dropped,
		duplicated,
		underrun,
		priming: false,
		nextPriming: false,
		readFrame: localRead
	};
}

function sabPull(control, ring, capacity, primingState, left, right) {
	let priming = primingState.value;
	const fill = sabFill(control, capacity);
	let readFrame = Atomics.load(control, SAB_READ);

	if (priming) {
		left.fill(0);
		right.fill(0);
		if (fill >= TARGET_FRAMES) priming = false;
		primingState.value = priming;
		// `priming: true` unconditionally, matching CueBridgeRing.pull(): this
		// return describes the call just made (it output silence), not the state
		// AFTER it - those differ on exactly the call that reaches target, which
		// is the call a caller most needs to classify correctly.
		return { dropped: false, duplicated: false, underrun: false, fill, priming: true };
	}

	const result = pullPolicy(fill, false, readFrame, ring, capacity, left, right);
	Atomics.store(control, SAB_READ, result.readFrame);
	if (result.underrun) Atomics.add(control, SAB_UNDERRUN, 1);
	primingState.value = result.nextPriming;
	// `priming: false` unconditionally, matching CueBridgeRing.pull(): a call
	// that reached here was NOT priming when it started, even if it underran
	// and just set the NEXT call to be one (that is `nextPriming`/the getter
	// this function's caller updates from `primingState.value`, a different
	// claim from what THIS call did).
	return {
		dropped: result.dropped,
		duplicated: result.duplicated,
		underrun: result.underrun,
		fill: result.fill,
		priming: false
	};
}

class CueBridgeSenderProcessor extends AudioWorkletProcessor {
	constructor(options) {
		super();
		const settings = (options && options.processorOptions) || {};
		this.mode = settings.mode;
		this.bridgePort = null;
		this._scratch = new Float32Array(RENDER_QUANTUM * 2);
		if (this.mode === 'sab') {
			this.control = new Int32Array(settings.controlBuffer);
			this.ring = new Float32Array(settings.controlBuffer, SAB_HEADER_BYTES);
			this.capacity = this.ring.length / 2;
		} else if (this.mode === 'port') {
			this.port.onmessage = (event) => {
				if (event.data?.type === 'connect' && event.data.port) {
					this.bridgePort = event.data.port;
				}
			};
		} else {
			throw new RangeError('cue bridge sender requires processorOptions.mode sab or port');
		}
	}

	process(inputs) {
		const channels = inputs[0];
		if (!channels || !channels[0] || !channels[1]) return true;
		const left = channels[0];
		const right = channels[1];
		if (left.length === 0) return true;
		if (this.mode === 'sab') {
			sabPush(this.control, this.ring, this.capacity, left, right);
		} else if (this.bridgePort) {
			const chunk = interleaveStereo(left, right, this._scratch);
			this.bridgePort.postMessage(chunk.slice(0));
		}
		return true;
	}
}

class CueBridgeReceiverProcessor extends AudioWorkletProcessor {
	constructor(options) {
		super();
		const settings = (options && options.processorOptions) || {};
		this.mode = settings.mode;
		this.bridgePort = null;
		this._priming = true;
		if (this.mode === 'sab') {
			this.control = new Int32Array(settings.controlBuffer);
			this.ring = new Float32Array(settings.controlBuffer, SAB_HEADER_BYTES);
			this.capacity = this.ring.length / 2;
			this._primingState = { value: true };
		} else if (this.mode === 'port') {
			this.ringLocal = new Float32Array((settings.capacity || TARGET_FRAMES + DRIFT_BAND * 4) * 2);
			this.capacity = this.ringLocal.length / 2;
			this.writePos = 0;
			this.readPos = 0;
			this.fill = 0;
			this.port.onmessage = (event) => {
				if (event.data?.type === 'connect' && event.data.port) {
					this.bridgePort = event.data.port;
					this.bridgePort.onmessage = (portEvent) => {
						const chunk = portEvent.data;
						if (!(chunk instanceof Float32Array) || chunk.length % 2 !== 0) return;
						for (let i = 0; i < chunk.length; i += 2) {
							if (this.fill >= this.capacity) {
								continue;
							}
							const slot = this.writePos * 2;
							this.ringLocal[slot] = chunk[i];
							this.ringLocal[slot + 1] = chunk[i + 1];
							this.writePos = (this.writePos + 1) % this.capacity;
							this.fill += 1;
						}
					};
					return;
				}
			};
		} else {
			throw new RangeError('cue bridge receiver requires processorOptions.mode sab or port');
		}
	}

	_pullPort(left, right) {
		if (this._priming) {
			left.fill(0);
			right.fill(0);
			if (this.fill >= TARGET_FRAMES) this._priming = false;
			// `priming: true` unconditionally: see the matching comment in
			// sabPull. This return is about the call just made, not the state
			// this.fill just left it in.
			return { dropped: false, duplicated: false, underrun: false, fill: this.fill, priming: true };
		}

		const result = pullPolicy(this.fill, false, this.readPos, this.ringLocal, this.capacity, left, right);
		this.readPos = result.readFrame;
		this.fill = result.fill;
		if (result.underrun) {
			this._priming = true;
			this.port.postMessage({ type: 'underrun' });
		} else if (result.nextPriming) {
			this._priming = true;
		}
		// `priming: false` unconditionally: see the matching comment in sabPull.
		// A call that reached here was not priming when it started; whether it
		// just set `this._priming` for the NEXT call is a different claim, read
		// off `this._priming` itself, not off this return value.
		return {
			dropped: result.dropped,
			duplicated: result.duplicated,
			underrun: result.underrun,
			fill: this.fill,
			priming: false
		};
	}

	process(inputs, outputs) {
		const output = outputs[0];
		if (!output || !output[0] || !output[1]) return true;
		const left = output[0];
		const right = output[1];
		// `process()` must return a plain boolean per the Web Audio spec, so the
		// per-call pull result (in particular its own `priming` flag, which is
		// not the same claim as the CURRENT `this._priming`/`this.fill` once the
		// call has run) is stashed here rather than discarded. Nothing in the
		// live bridge reads it; it exists so a parity test can compare it
		// against `CueBridgeRing.pull()`'s own return value.
		if (this.mode === 'sab') {
			this._lastPull = sabPull(this.control, this.ring, this.capacity, this._primingState, left, right);
			this._priming = this._primingState.value;
		} else {
			this._lastPull = this._pullPort(left, right);
		}
		return true;
	}
}

registerProcessor('cue-bridge-sender', CueBridgeSenderProcessor);
registerProcessor('cue-bridge-receiver', CueBridgeReceiverProcessor);
