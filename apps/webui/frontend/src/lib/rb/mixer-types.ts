/**
 * Mixer surface contract: channel strips, crossfader routing, and the real
 * headphone cue bus.
 *
 * Split out of the former lib/rb/types.ts god module.
 */

import type { DeckId } from './deck-slots';

/** EQ band selector for AudioEngine.setEq. */
export type EqBand = 'low' | 'mid' | 'high';

/** Crossfader bus assignment for one channel.
 * 'A' = left bus, 'B' = right bus, 'THRU' = bypass the crossfader. */
export type CrossfaderAssign = 'A' | 'B' | 'THRU';

/** One mixer channel strip. Channel order on screen is 3 1 2 4. */
export interface MixerChannelState {
	/** The deck this strip controls. */
	deck_id: DeckId;
	/** TRIM knob 0..1; 0.5 = unity gain. Engine maps to input GainNode. */
	trim: number;
	/** HIGH knob 0..1; 0.5 = flat. Engine maps to highshelf BiquadFilter dB. */
	eq_high: number;
	/** MID knob 0..1; 0.5 = flat. Engine maps to peaking BiquadFilter dB. */
	eq_mid: number;
	/** LOW knob 0..1; 0.5 = flat. Engine maps to lowshelf BiquadFilter dB. */
	eq_low: number;
	/** FILTER knob 0..1; 0.5 = bypass (dead zone). Below 0.5 sweeps a lowpass
	 * closed toward FILTER_LP_FLOOR_HZ; above 0.5 sweeps a highpass closed
	 * toward FILTER_HP_CEILING_HZ. See player/constants.ts. */
	filter: number;
	/** Vertical channel fader 0..1; 1 = full. Engine maps to fader GainNode. */
	fader: number;
	/** Crossfader bus assignment (the 2x2 numeral matrices). */
	assign: CrossfaderAssign;
	/** Headphone pre-fader cue assignment for this channel. */
	cue_enabled: boolean;
}

/** One real browser-selectable audio output. Labels may be empty until the
 * browser grants device-label permission. */
export interface HeadphoneOutputDevice {
	id: string;
	label: string;
}

/** Serializable headphone cue-bus read model. `active` means the monitor
 * stream is attached to the element and the selected sink accepted playback. */
export interface HeadphoneState {
	mix: number;
	level: number;
	selected_output_device_id: string | null;
	outputs: HeadphoneOutputDevice[];
	supported: boolean;
	active: boolean;
	error: string | null;
}

/** Whole mixer surface including the real headphone cue bus. */
export interface MixerState {
	/** All four channel strips keyed by deck. */
	channels: Record<DeckId, MixerChannelState>;
	/** Horizontal crossfader position 0..1; 0 = full A, 1 = full B. */
	crossfader: number;
	/** Master volume 0..1 (topbar horizontal slider -> master GainNode). */
	master: number;
	/** Headphone cue / monitor output state. */
	headphones: HeadphoneState;
}
