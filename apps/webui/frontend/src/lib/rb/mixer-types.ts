/**
 * Mixer surface contract: channel strips, crossfader routing, and the real
 * headphone cue bus.
 *
 * Split out of the former lib/rb/types.ts god module.
 */

import type { HeadphoneAlignmentMode } from '$lib/player/constants';
import type { LivenessVerdict } from '$lib/rb/audio-output-liveness';
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
	/** MIXUX-04: HI/MID/LOW knobs control stem levels when true. */
	stem_eq_mode: boolean;
}

/** One real browser-selectable audio output. Labels may be empty until the
 * browser grants device-label permission. */
export interface HeadphoneOutputDevice {
	id: string;
	label: string;
	/** CUEOUT-26, Mac shell only: the id of the output that actually sounds
	 * for this one (the occupied headphone jack for the muted MacBook
	 * speakers). Absent in a browser listing, where every id is its own. */
	physical_id?: string;
	/** CUEOUT-26, Mac shell only: the MacBook speakers while headphones occupy the jack. */
	muted_by_jack?: boolean;
	/** CoreAudio transport from the Mac shell (`builtin`, `usb`, `virtual`, ...). */
	transport?: string;
}

/** CUEOUT-26: why the cue runs as split cue. `same_device`: MAIN and CUE are
 * one physical output, so master plays L and cue R on it. */
export type HeadphoneSplitReason = 'same_device';

/** Headphone output routing. Unknown modes fail fast. */
export type HeadphoneOutputMode = 'practice' | 'two_outputs' | 'split_cable';

/** CUEOUT-14: how a measured cue/master offset is split between HEAD DELAY
 * and the room delay. One source of truth: `HEADPHONE_ALIGNMENT_MODES` in
 * player/constants.ts (a leaf), re-exported here beside the state it types. */
export type { HeadphoneAlignmentMode };

/** CUEOUT-14 calibration modal step. `idle` before a run and after an abort. */
export type CueAlignStep =
	| 'idle'
	| 'mic_access'
	| 'mic_check_master'
	| 'mic_check_cue'
	| 'measuring'
	| 'verifying'
	| 'applied'
	| 'failed';

/** One production calibration probe.  The historic implementation only has a
 * chirp train; `alternate_probe` makes that absence visible instead of
 * offering a no-op selector. */
export type CueCalibrationProbe = 'chirp';
export type CueCalibrationFailure =
	| 'no_input_signal'
	| 'weak_correlation'
	| 'inconsistent_measurements'
	| 'microphone_access'
	| 'route_or_operation';

/** Bounded, serializable evidence from the current/last calibration run.
 * These are measured summaries, never a claim that sound reached a physical
 * speaker or headphone. */
export interface HeadphoneCalibrationDiagnostics {
	probe: CueCalibrationProbe;
	alternate_probe: 'unavailable';
	failure: CueCalibrationFailure | null;
	master_measurements_ms: number[];
	cue_measurements_ms: number[];
	spread_ms: number | null;
}

/** One stage-one attempt, in the serialized read model. The snake_case twin of
 * `CueAlignProbe` (player/cue-align.svelte.ts), which is the controller's own
 * camelCase shape. It lives here because the ear-cup step is interactive: the
 * operator moves the cup and watches `best` climb toward `threshold`, so an
 * agent driving the same step over IPC or HTTP needs the same numbers. Null
 * between runs and outside stage one. */
export interface HeadphoneCalibrationProbe {
	bus: 'cue' | 'master';
	gain: number;
	/** Null while this rung is still playing. */
	peak: number | null;
	/** What this attempt measured, null while it is still playing. */
	lag_ms: number | null;
	best: number;
	threshold: number;
}

/** Live calibration progress, mirrored as `GET /headphones.calibration`.
 * Seeded from the persisted last calibration on load so the offset survives a
 * reload; the latencies are null until a run has measured them. */
export interface HeadphoneCalibrationState {
	step: CueAlignStep;
	cue_latency_ms: number | null;
	master_latency_ms: number | null;
	/** `cue_latency_ms - master_latency_ms`: positive means the phones are behind the room. */
	offset_ms: number | null;
	/** Residual cue-minus-master offset after the post-apply verification chirp, null until verified. */
	verify_residual_ms: number | null;
	/** Live stage-one level find. What the modal's bar draws, so an agent sees it too. */
	probe: HeadphoneCalibrationProbe | null;
	error: string | null;
	diagnostics: HeadphoneCalibrationDiagnostics;
}

export type HeadphoneSignalState = 'inactive' | 'unavailable' | 'measured';
/** A measured internal app bus or captured microphone block.  It explicitly
 * cannot prove physical acoustic output. */
export interface HeadphoneSignal {
	state: HeadphoneSignalState;
	rms: number | null;
	peak: number | null;
	measured_at: string | null;
	source: 'application_bus' | 'captured_input';
	physical_output_proven: false;
}

export interface HeadphoneSignals {
	master: HeadphoneSignal;
	cue: HeadphoneSignal;
	input: HeadphoneSignal;
}

/** Route capability is kept apart from CUE graph activity: an active CUE
 * stream does not establish that a separate MAIN sink was accepted. */
export interface HeadphoneRouteHealth {
	state: 'default' | 'selected' | 'unsupported' | 'failed';
	selected: boolean;
}

/** Serializable headphone cue-bus read model. `active` means the monitor
 * stream is attached to the element and the selected sink accepted playback. */
/** IOPIN-14: the outcome of the last attempt to list audio devices.
 * `listed` is the only state in which the lists are the machine's real device
 * names; every other state names why they are not, so an unreadable list is
 * never drawn as an empty one. */
export type IoDeviceAccessStatus =
	| 'not_checked'
	| 'listed'
	| 'permission_needed'
	| 'permission_denied'
	| 'api_missing'
	| 'enumeration_failed'
	| 'timeout';

/** What the panel offers next to the state: `grant` opens the permission
 * flow, `retry` lists again, `none` means there is nothing to fix. */
export type IoDeviceAccessAction = 'none' | 'grant' | 'retry';

export interface IoDeviceAccess {
	status: IoDeviceAccessStatus;
	action: IoDeviceAccessAction;
	/** Operator-facing sentence for the state; null only when `listed`. */
	message: string | null;
	/** The underlying error text for a failed attempt, else null. */
	detail: string | null;
	/** False when this browser or shell cannot pin an output (no
	 * `AudioContext.setSinkId`): audio follows the OS default output. */
	output_pinning: boolean;
	/** Standing notices: a saved device that is absent, an unpinnable shell. */
	notices: string[];
}

export interface HeadphoneState {
	mix: number;
	level: number;
	selected_output_device_id: string | null;
	/** `practice` blends PFL into the main output when no monitor is selected.
	 * `split_cable` sends mono master on L and mono cue on R of the main output.
	 * Selecting a monitor writes `two_outputs` and restores master-only main. */
	output_mode: HeadphoneOutputMode;
	/** Mixxx Head Delay, milliseconds, 0..500. Applied as a DelayNode after level on the monitor path. */
	head_delay_ms: number;
	/** CUEOUT-14: which side of the cue/master offset gets delayed. Persisted. */
	alignment_mode: HeadphoneAlignmentMode;
	/** CUEOUT-14: room (MASTER) delay, milliseconds, 0..1500, the LAST node
	 * before the destination. The monitor tap is upstream, so the phones never
	 * pay it. Persisted; shown as `ROOM +N ms`. */
	master_delay_ms: number;
	/** CUEOUT-14: live calibration progress and the last measured offset. */
	calibration: HeadphoneCalibrationState;
	/** Actual analyser/capture observations. Null levels are unavailable or inactive, never animated estimates. */
	signals: HeadphoneSignals;
	routes: { master: HeadphoneRouteHealth; cue: HeadphoneRouteHealth };
	outputs: HeadphoneOutputDevice[];
	inputs: HeadphoneOutputDevice[];
	/** Room / MASTER sink (`AudioContext.setSinkId`). Null follows the OS default. */
	selected_master_output_device_id: string | null;
	/** Label-unlock and any later capture; never defaulted to a headphone/HFP mic. */
	selected_input_device_id: string | null;
	supported: boolean;
	active: boolean;
	error: string | null;
	/** IOPIN-14: whether the device lists above could be read at all, and what
	 * the operator can do when they could not. Never inferred from an empty list. */
	device_access: IoDeviceAccess;
	/** Live output liveness verdict from the monitor path; absent until probed. */
	liveness_verdict?: LivenessVerdict;
	/** CUEOUT-26: set while split cue was chosen because MAIN and CUE are one
	 * physical output; null or absent when the operator picked the mode. */
	split_reason?: HeadphoneSplitReason | null;
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
