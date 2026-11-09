/**
 * Spec-faithful AudioParam stand-in for the filter corner glide tests (FXFILT-10611).
 *
 * Models the parts of https://webaudio.github.io/web-audio-api/#AudioParam a
 * corner glide touches: cancelScheduledValues removes every event at or after
 * the cancel time AND a setValueCurveAtTime curve that is active at it;
 * overlapping curves throw; frames rendered with renderTo are frozen, so a
 * later cancel never rewrites them. tests/e2e/filter-glide-real-audio.spec.ts
 * checks this stand-in against Chromium's own AudioParam.
 */
export class SpecAudioParam {
	/**
	 * @param {number} defaultValue intrinsic value before any automation event
	 * @param {number} sampleRate frames per second for renderTo
	 */
	constructor(defaultValue, sampleRate) {
		this.value = defaultValue;
		this.sampleRate = sampleRate;
		this.calls = [];
		this.events = [];
		this.rendered = [];
	}

	cancelScheduledValues(t) {
		this.calls.push(['cancel', t]);
		this.events = this.events.filter(
			(e) => e.t < t && !(e.kind === 'curve' && t < e.t + e.duration)
		);
	}

	setTargetAtTime(v, t, tau) {
		this.calls.push(['target', v, t, tau]);
		if (![v, t, tau].every(Number.isFinite) || tau < 0)
			throw new RangeError(`bad setTargetAtTime(${v}, ${t}, ${tau})`);
		this._assertOutsideCurves(t, t);
		this._insert({ kind: 'target', v, t, tau });
	}

	setValueCurveAtTime(curve, t, duration) {
		this.calls.push(['curve', curve.length, t, duration]);
		if (!(duration > 0) || curve.length < 2)
			throw new RangeError(`bad setValueCurveAtTime(len ${curve.length}, ${t}, ${duration})`);
		this._assertOutsideCurves(t, t + duration);
		for (const e of this.events) {
			if (e.t >= t && e.t < t + duration)
				throw new Error(`NotSupportedError: event at ${e.t} inside new curve [${t}, ${t + duration})`);
		}
		this._insert({ kind: 'curve', curve: Float32Array.from(curve), t, duration });
	}

	/** Value of the CURRENT timeline at `t`; each event starts from the value the timeline has at its own time. */
	valueAt(t) {
		let v = this.value;
		let active = null;
		for (const e of this.events) {
			if (e.t > t) break;
			if (active !== null) v = this._eval(active, e.t, v);
			active = e;
		}
		return active === null ? v : this._eval(active, t, v);
	}

	/** Render frames up to (not including) `frame`; frames already rendered never change. */
	renderTo(frame) {
		for (let f = this.rendered.length; f < frame; f++) this.rendered.push(this.valueAt(f / this.sampleRate));
	}

	_insert(event) {
		const i = this.events.findIndex((e) => e.t > event.t);
		if (i === -1) this.events.push(event);
		else this.events.splice(i, 0, event);
	}

	_assertOutsideCurves(start, end) {
		for (const e of this.events) {
			if (e.kind !== 'curve') continue;
			const inside = (x) => x >= e.t && x < e.t + e.duration;
			if (inside(start) || (end > start && start < e.t && end > e.t))
				throw new Error(`NotSupportedError: [${start}, ${end}) overlaps curve at ${e.t}`);
		}
	}

	_eval(e, t, v0) {
		if (e.kind === 'target') return e.v + (v0 - e.v) * Math.exp(-(t - e.t) / e.tau);
		const n = e.curve.length;
		if (t >= e.t + e.duration) return e.curve[n - 1];
		const pos = ((t - e.t) / e.duration) * (n - 1);
		const i = Math.floor(pos);
		return e.curve[i] + (e.curve[i + 1] - e.curve[i]) * (pos - i);
	}
}
