/**
 * Human-readable aria-valuetext for mixer sliders (issue #10365, A11Y-11269).
 * Pure formatters; no DOM.
 */

import {
	TRIM_MAX_GAIN,
	FILTER_DEADZONE_FRAC,
	assertUnitRange,
	eqDbFromKnob
} from '$lib/player/constants';

export type ValueTextKind =
	'eq' | 'trim' | 'filter' | 'bipolar' | 'fader' | 'crossfader' | 'pitch' | 'percent';

function requireFinite(value: number, kind: ValueTextKind): void {
	if (!Number.isFinite(value)) {
		throw new RangeError(`valueText(${kind}) must be finite, got ${value}`);
	}
}

/** Signed dB with one decimal, no sign on zero, ASCII minus. */
function formatSignedDb(db: number): string {
	if (!Number.isFinite(db)) {
		throw new RangeError(`formatSignedDb expected finite dB, got ${db}`);
	}
	const rounded = Math.round(db * 10) / 10;
	if (rounded === 0 || Object.is(rounded, -0)) {
		return '0 dB';
	}
	const body = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
	return rounded > 0 ? `+${body} dB` : `${body} dB`;
}

function filterColour(value: number): number {
	return (value - 0.5) * 2;
}

function bipolarOrFilterText(value: number, kind: 'filter' | 'bipolar'): string {
	const colour = filterColour(value);
	if (Math.abs(colour) < FILTER_DEADZONE_FRAC) {
		return 'off';
	}
	const pct = Math.round(Math.abs(colour) * 100);
	if (kind === 'filter') {
		return colour < 0 ? `low-pass ${pct}%` : `high-pass ${pct}%`;
	}
	return colour < 0 ? `${pct}% counter-clockwise` : `${pct}% clockwise`;
}

export function valueText(kind: ValueTextKind, value: number): string {
	requireFinite(value, kind);
	if (kind !== 'pitch') {
		assertUnitRange('valueText', value);
	}

	switch (kind) {
		case 'eq':
			return formatSignedDb(eqDbFromKnob(value));
		case 'trim':
			if (value === 0) return '-∞ dB';
			return formatSignedDb(20 * Math.log10(TRIM_MAX_GAIN * value));
		case 'filter':
			return bipolarOrFilterText(value, 'filter');
		case 'bipolar':
			return bipolarOrFilterText(value, 'bipolar');
		case 'fader':
		case 'percent':
			return `${Math.round(value * 100)}%`;
		case 'crossfader': {
			const pct = Math.round(Math.abs(value - 0.5) * 200);
			if (pct === 0) return 'centre';
			return value < 0.5 ? `${pct}% left` : `${pct}% right`;
		}
		case 'pitch': {
			const s = value.toFixed(2);
			if (Number(s) === 0) return '0.00%';
			return value > 0 ? `+${s}%` : `${s}%`;
		}
		default: {
			const _exhaustive: never = kind;
			throw new Error(`Unhandled valueText kind: ${_exhaustive}`);
		}
	}
}

/** Maps a knob registry id (`deck:role`) to the formatter kind. */
export function knobValueKind(knobId: string): ValueTextKind {
	const colon = knobId.lastIndexOf(':');
	const role = colon >= 0 ? knobId.slice(colon + 1) : knobId;
	if (role === 'trim') return 'trim';
	if (role === 'high' || role === 'mid' || role === 'low') return 'eq';
	if (role === 'filter') return 'filter';
	return 'percent';
}
