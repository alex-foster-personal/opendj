/**
 * WAVE-EQ-5230: per-deck waveform band gains, as the audio engine reports them.
 *
 * The Rust engine's state feed carries `wave_gains` per deck: the linear
 * magnitude of that deck's channel strip (EQ low/mid/high, then the FILTER
 * knob) at one representative frequency per ANLZ band
 * (`apps/audio-engine/src/wave_gains.rs`). The waveform painters multiply each
 * band's amplitude by its gain, so a low kill visibly drains the lows.
 *
 * The engine is the only source: this module never derives gains from knob
 * positions itself. With no engine report (the Web Audio engine, or before the
 * first state message) a deck's gains are null and the waveform draws the
 * analysis unscaled, which is what that deck shows today.
 */
import { rustMode } from '$lib/audio-engine/rust-mode.svelte';
import type { DeckId } from './deck-id';

/** Linear amplitude gain per ANLZ band; 1 is untouched, 0 is silent. */
export interface BandGains {
	readonly low: number;
	readonly mid: number;
	readonly high: number;
}

/** Last engine-reported gains per deck; null when the engine has not reported. */
export const waveEqGains = $state<Record<DeckId, BandGains | null>>({
	1: null,
	2: null,
	3: null,
	4: null
});

function _gain(raw: Record<string, unknown>, band: keyof BandGains): number {
	const v = raw[band];
	if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) {
		throw new TypeError(`wave_gains.${band} must be a finite number >= 0, got ${String(v)}`);
	}
	return v;
}

/** Parse the engine's `wave_gains` object. Throws on a malformed one: an engine
 * that stopped reporting is a contract break, never a flat waveform. */
export function parseWaveGains(raw: unknown): BandGains {
	if (typeof raw !== 'object' || raw === null) {
		throw new TypeError(`wave_gains must be an object, got ${String(raw)}`);
	}
	const r = raw as Record<string, unknown>;
	return Object.freeze({ low: _gain(r, 'low'), mid: _gain(r, 'mid'), high: _gain(r, 'high') });
}

/** Store a deck's gains. The object is replaced only when a value changed, so
 * painters can compare by identity to decide whether to repaint. */
export function setDeckWaveGains(deck: DeckId, gains: BandGains | null): void {
	const prev = waveEqGains[deck];
	if (gains === null || prev === null) {
		if (gains !== prev) waveEqGains[deck] = gains;
		return;
	}
	if (prev.low === gains.low && prev.mid === gains.mid && prev.high === gains.high) return;
	waveEqGains[deck] = gains;
}

/** Forget reported gains: one deck (its track was loaded or unloaded) or every
 * deck (a new engine connection, whose state has not arrived yet). */
export function clearWaveGains(deck: DeckId | null = null): void {
	for (const d of deck === null ? ([1, 2, 3, 4] as const) : [deck]) setDeckWaveGains(d, null);
}

/** The gains a painter should apply: null (draw unscaled) when the setting is
 * off, the page is not on the Rust engine (so a Web Audio page never paints
 * stale Rust gains), or the engine has not reported for this deck's load. */
export function paintBandGains(deck: DeckId, enabled: boolean): BandGains | null {
	return enabled && rustMode.enabled ? waveEqGains[deck] : null;
}
