/** Test-only acoustic-lag fixtures for CUEOUT-11. Production DSP lives in cue-latency.ts. */

function delaySignal(signal, delaySamples, tailSamples = 0) {
	if (!Number.isInteger(delaySamples) || delaySamples < 0) {
		throw new RangeError(`delaySamples must be a non-negative integer, got ${delaySamples}`);
	}
	if (!Number.isInteger(tailSamples) || tailSamples < 0) {
		throw new RangeError(`tailSamples must be a non-negative integer, got ${tailSamples}`);
	}
	const out = new Float32Array(delaySamples + signal.length + tailSamples);
	out.set(signal, delaySamples);
	return out;
}

export function addGaussianNoise(signal, rms, seed = 1) {
	if (!Number.isFinite(rms) || rms < 0) {
		throw new RangeError(`noise rms must be >= 0, got ${rms}`);
	}
	const out = new Float32Array(signal.length);
	let s = seed >>> 0;
	for (let i = 0; i < signal.length; i += 1) {
		s = (1664525 * s + 1013904223) >>> 0;
		const u1 = ((s >>> 8) + 1) / 16777216;
		s = (1664525 * s + 1013904223) >>> 0;
		const u2 = ((s >>> 8) + 1) / 16777216;
		const gauss = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
		out[i] = signal[i] + gauss * rms;
	}
	return out;
}

export function delayedCapture(reference, delayMs, sampleRate, tailMs = 40) {
	const delaySamples = Math.round((delayMs / 1000) * sampleRate);
	return delaySignal(reference, delaySamples, Math.round((tailMs / 1000) * sampleRate));
}
