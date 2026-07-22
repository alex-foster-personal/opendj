/**
 * Pitch fader value <-> tempo ratio mapping (pitch-slider, ledger issue 186).
 *
 * Requirements:
 *   ✔︎ A centered-zero fader value of 0.5 always means 0% pitch (ratio 1.0),
 *     independent of the selected +-8/+-16/WIDE range.
 *     [if] value is 0.5 [then] ratio is 1.0 for every pitchRangePct
 *   ✔︎ The fader's full 0..1 travel always spans exactly the selected range.
 *     [if] pitchRangePct is 8 and value is 1 [then] ratio is 1.08
 *     [if] pitchRangePct is 8 and value is 0 [then] ratio is 0.92
 *   ✔︎ Round-tripping a ratio through faderValueFromPitchRatio then back
 *     through pitchRatioFromFaderValue recovers the original ratio.
 */

function _assertPositive(name: string, value: number): void {
	if (!Number.isFinite(value) || value <= 0) {
		throw new RangeError(`${name} must be finite and positive, got ${value}`);
	}
}

function _clamp01(value: number): number {
	return Math.min(1, Math.max(0, value));
}

export function pitchRatioFromFaderValue(value: number, pitchRangePct: number): number {
	_assertPositive('pitchRangePct', pitchRangePct);
	if (!Number.isFinite(value)) throw new RangeError(`value must be finite, got ${value}`);
	return 1 + (_clamp01(value) - 0.5) * 2 * (pitchRangePct / 100);
}

export function faderValueFromPitchRatio(ratio: number, pitchRangePct: number): number {
	_assertPositive('pitchRangePct', pitchRangePct);
	if (!Number.isFinite(ratio) || ratio <= 0) {
		throw new RangeError(`ratio must be finite and positive, got ${ratio}`);
	}
	return _clamp01(0.5 + ((ratio - 1) * 100) / (2 * pitchRangePct));
}
