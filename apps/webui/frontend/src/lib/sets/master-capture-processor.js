/**
 * The set recorder's master-mix tap, as an AudioWorkletProcessor (SET-12).
 *
 * Connected from the master bus (`_masterGain`: post master fader, before the
 * room delay, the `?muted=1` belt and the device; never the headphone cue).
 * It converts each quantum to interleaved int16 stereo into a preallocated
 * chunk and transfers the chunk to the main thread when full; the main thread
 * posts it to the daemon, which writes the WAV.
 *
 * AUDIO-THREAD COST, kept to a clamp, a multiply and a store per sample:
 *   - no allocation per quantum. Chunks come from a pool the main thread
 *     refills by transferring each buffer back after its POST, so steady state
 *     allocates nothing; a new buffer is made only while the pool is empty
 *     (start-up, or a slow POST), at most once per chunk, never per quantum;
 *   - no per-quantum messages: one transfer per chunk (`chunkFrames`).
 *
 * A quantum with no active input (every deck stopped: Chrome hands an empty
 * input) is written as silence, so the file's time stays the set's time.
 *
 * It never writes audio: its one output stays silent and the main thread
 * connects it through a zero gain, so a bug here cannot reach the speakers.
 * Plain JS with no imports, like meter-processor.js: a worklet scope cannot
 * resolve modules.
 */

/* global AudioWorkletProcessor, registerProcessor */

class MasterCaptureProcessor extends AudioWorkletProcessor {
	constructor(options) {
		super();
		const frames = options && options.processorOptions && options.processorOptions.chunkFrames;
		if (!Number.isInteger(frames) || frames < 128) {
			throw new RangeError(`master capture needs an integer chunkFrames >= 128, got ${frames}`);
		}
		this.chunkFrames = frames;
		this.pool = [];
		this.chunk = new Int16Array(frames * 2);
		this.filled = 0;
		this.flushed = false;
		this.port.onmessage = (event) => {
			const data = event.data;
			if (data && data.reuse instanceof ArrayBuffer && data.reuse.byteLength === frames * 4) {
				this.pool.push(new Int16Array(data.reuse));
			} else if (data && data.flush === true) {
				this.send();
				this.flushed = true;
				this.port.postMessage({ flushed: true });
			}
		};
	}

	send() {
		if (this.filled === 0) return;
		const full = this.chunk;
		const frames = this.filled;
		this.chunk = this.pool.length > 0 ? this.pool.pop() : new Int16Array(this.chunkFrames * 2);
		this.filled = 0;
		this.port.postMessage({ pcm: full.buffer, frames }, [full.buffer]);
	}

	process(inputs) {
		if (this.flushed) return false;
		const input = inputs[0];
		const left = input && input.length > 0 ? input[0] : null;
		const right = input && input.length > 1 ? input[1] : left;
		const quantum = left === null ? 128 : left.length;
		let read = 0;
		while (read < quantum) {
			const take = Math.min(this.chunkFrames - this.filled, quantum - read);
			const out = this.chunk;
			let at = this.filled * 2;
			if (left === null) {
				out.fill(0, at, at + take * 2);
			} else {
				for (let i = read; i < read + take; i += 1) {
					const l = left[i];
					const r = right[i];
					out[at] = Math.round((l > 1 ? 1 : l < -1 ? -1 : l) * 32767);
					out[at + 1] = Math.round((r > 1 ? 1 : r < -1 ? -1 : r) * 32767);
					at += 2;
				}
			}
			this.filled += take;
			read += take;
			if (this.filled === this.chunkFrames) this.send();
		}
		return true;
	}
}

registerProcessor('mdt-master-capture', MasterCaptureProcessor);
