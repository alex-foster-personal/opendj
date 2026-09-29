/**
 * The audio engine's public method contract.
 *
 * Split out of the former lib/rb/types.ts god module: it is the one entry
 * there that is a behavioral contract rather than a data shape, and it sits
 * downstream of every other module in this group.
 */

import type { DeckId } from './deck-slots';

export type MasterMode = 'auto' | 'locked';

/** Where an armed jump lands on the deck's own transport: a fixed position, or
 * a resolver the engine calls with its LIVE presentation position inside the
 * scheduling transaction, so a published position that went stale while the
 * command queued can never pick an arm point already behind the playhead. */
export type ArmAtPosition = number | ((nowPositionSec: number) => number);

export type MasterReason =
	| 'manual'
	| 'play-claim'
	| 'first-claim'
	| 'master-left'
	| 'unload'
	| 'natural-end'
	| 'beat-sync-enable'
	| 'unlock-reelect'
	| 'dispose'
	| null;

export interface DeckLoadOptions {
	/** Whether the asynchronous stem probe and decode may run for this load.
	 * `undefined` means absent (true), so a dispatcher can pass its own
	 * optional field straight through. */
	stems?: boolean | undefined;
	/** The caller shows its own toast for a failed load (Trackify, #4036). The
	 * engine still reports the failure and its stage context to the server. */
	suppressFailureToast?: boolean | undefined;
}
import type { DeckAudioSnapshot, QuantizeGrid, SyncMode } from './deck-state-types';
import type { CrossfaderAssign, EqBand, HeadphoneAlignmentMode, HeadphoneOutputMode } from './mixer-types';
import type { StemControl } from './stem-types';

/**
 * Public interface of the client-side Web Audio engine (build unit
 * audio-engine). Graph per deck: source -> TRIM gain -> 3-band EQ
 * (lowshelf/peaking/highshelf) -> channel fader gain -> crossfader gain ->
 * master gain -> destination. The implementing rune store MUST live in a
 * .svelte.ts module (rune_outside_svelte otherwise - RECON-FRONTEND 10.1).
 *
 * Fail-fast contract: every method throws (or rejects) loudly on invalid
 * input or missing backing data - no silent no-ops, no fabricated audio.
 */
export interface AudioEngine {
	/** Stop and release all route-owned processors, nodes, clocks, and context. */
	dispose(): Promise<void>;
	/** Fetch /tracks/{sid}/audio, decodeAudioData, build the deck chain and
	 * populate DeckState. Rejects with the backend error code on 404
	 * (AUDIO_FILE_MISSING / AUDIO_IS_STREAMING_URI / TRACK_NOT_FOUND). */
	load(deck: DeckId, stable_id: string, options?: DeckLoadOptions): Promise<void>;
	/** Start/resume transport from the current position. Throws if no track
	 * is loaded on the deck. */
	play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void>;
	/** Pause transport, keeping position. Throws if no track loaded. */
	pause(deck: DeckId): Promise<void>;
	/** Seek to a position in ms (hot-cue click / CUE return). Implemented as
	 * buffer-source restart at offset. Throws if no track loaded. */
	cueJump(deck: DeckId, ms: number): Promise<void>;
	/** Set playback-rate ratio from the pitch fader. Validates 0 < ratio and
	 * that it fits the deck's selected pitch range. */
	setPitch(deck: DeckId, ratio: number): Promise<void>;
	/** Semantic alias for setPitch: ratio controls tempo, while Master Tempo
	 * independently controls whether pitch is preserved. */
	setTempoRatio(deck: DeckId, ratio: number): Promise<void>;
	/** Engage a loop range (ms) or disengage with null. */
	setLoop(deck: DeckId, loop: { in_ms: number; out_ms: number } | null): Promise<void>;
	/** Save current engaged loop as the one-slot safety loop (armed). */
	saveSafetyLoop(deck: DeckId): void;
	setSafetyLoopArmed(deck: DeckId, armed: boolean): void;
	clearSafetyLoop(deck: DeckId): void;
	/** Enable/disable PQTZ snapping. Defaults true per deck. */
	setQuantize(deck: DeckId, enabled: boolean): void;
	/** Change the quantize grid (1/4/8 beats). 'phase' is rejected upstream
	 * in performance-ipc, never reaches here. */
	setQuantizeGrid(deck: DeckId, beats: Exclude<QuantizeGrid, 'phase'>): void;
	/** Enable/disable master tempo/phase following. Defaults true per deck. */
	setBeatSync(deck: DeckId, enabled: boolean): Promise<void>;
	/** Enable/disable pitch preservation in the Signalsmith processor. */
	setMasterTempo(deck: DeckId, enabled: boolean): Promise<void>;
	/** Arm or disarm SLIP. Disarming active slip resumes through the normal schedule first. */
	setSlip(deck: DeckId, enabled: boolean): Promise<void>;
	/** Shift the loaded deck by exactly one semitone. */
	nudgeKey(deck: DeckId, semitones: -1 | 1): Promise<void>;
	/** Align the loaded deck to the elected loaded master using the documented
	 * deterministic Camelot harmonic policy. */
	syncKey(deck: DeckId): Promise<void>;
	/** Latch KEY SYNC on/off. Enabling applies syncKey; the control stays lit
	 * while enabled. Disabling restores the pre-latch manual key shift. */
	setKeySync(deck: DeckId, enabled: boolean): Promise<void>;
	/** Stop transport if needed and clear the deck so a replacement load can
	 * claim it. No-op when the deck is already empty. */
	unload(deck: DeckId): Promise<void>;
	/** Select beat or bar phase alignment for Beat Sync. */
	setSyncMode(deck: DeckId, mode: SyncMode): Promise<void>;
	/** Elect one loaded deck as the globally exclusive master. */
	setDeckMaster(deck: DeckId, options?: { lock?: boolean }): Promise<void>;
	/** Mute one real stem group. Rejects unless aligned artifacts are ready. */
	setStemMute(deck: DeckId, stem: StemControl, muted: boolean, pressT0Ms?: number): void;
	/** Solo one real stem group. Rejects unless aligned artifacts are ready. */
	setStemSolo(deck: DeckId, stem: StemControl, solo: boolean, pressT0Ms?: number): void;
	/** Per-stem level 0..1 (0.5 = unity). Rejects unless aligned artifacts are ready. */
	setStemGain(deck: DeckId, stem: StemControl, value: number): void;
	/** Toggle HI/MID/LOW between EQ and stem level for one channel strip. */
	setStemEqMode(deck: DeckId, enabled: boolean): void;
	/** Explicit seek entry point. cueJump delegates here so quantize is central.
	 * pressT0Ms is Q1's operator-felt press stamp, present on the hot-cue jump
	 * path and absent on a plain waveform seek. */
	quantizedSeek(deck: DeckId, ms: number, skipGridQuantize?: boolean, pressT0Ms?: number): Promise<void>;
	/** DECKUX-09: defer a hot-cue jump to `armAtPositionSec` on the deck's own
	 * transport clock; returns the absolute AudioContext time it lands at.
	 * pressT0Ms is Q1's operator-felt press stamp. */
	armHotCueTrigger(
		deck: DeckId,
		targetPositionMs: number,
		armAtPositionSec: ArmAtPosition,
		pressT0Ms?: number
	): Promise<number>;
	/** LATENCY-02: arm QUANTIZED LAUNCH on the follower's next shared beat 1. */
	armQuantizedLaunch(deck: DeckId, pressT0Ms?: number): Promise<number>;
	clearQuantizedLaunch(deck: DeckId): void;
	/** The engine's AudioContext clock, for projecting an armed trigger's
	 * remaining wait without exposing the context itself. */
	contextTimeNowSec(): number;
	/** Capture real post-Signalsmith analyser data; never synthesised. */
	captureDeckAudio(deck: DeckId): DeckAudioSnapshot;
	/** TRIM knob 0..1 (0.5 = unity) -> per-channel input GainNode. */
	setTrim(deck: DeckId, value: number): void;
	/** One EQ band knob 0..1 (0.5 = flat) -> Biquad gain in dB. */
	setEq(deck: DeckId, band: EqBand, value: number, pressT0Ms?: number): void;
	/** FILTER knob 0..1 (0.5 = bypass) -> paired lowpass/highpass Biquad
	 * corner frequencies. See player/constants.ts FILTER_*. */
	setFilter(deck: DeckId, value: number, pressT0Ms?: number): void;
	/** Channel fader 0..1 -> fader GainNode. */
	setFader(deck: DeckId, value: number, pressT0Ms?: number): void;
	/** Crossfader 0..1 (0 = full A, 1 = full B); applies the gain pair to
	 * every channel assigned A or B. */
	setCrossfader(value: number, pressT0Ms?: number): void;
	/** Route a channel to crossfader bus A, B, or THRU (bypass). */
	assignChannel(deck: DeckId, assign: CrossfaderAssign): void;
	/** Enable or disable a channel's post-EQ, pre-fader headphone cue tap. */
	setChannelCue(deck: DeckId, enabled: boolean): void;
	/** Set CUE-to-MASTER monitor mix and headphone level. */
	setHeadphoneMix(value: number): void;
	setHeadphoneLevel(value: number): void;
	setHeadDelayMs(value: number): void;
	/** CUEOUT-14: room delay line, 0..1500 ms, the last node before the destination. */
	setMasterDelayMs(value: number): void;
	/** CUEOUT-14: how a measured cue/master offset is split; re-applies the last calibration. */
	setHeadphoneAlignmentMode(mode: HeadphoneAlignmentMode): void;
	/** Practice, split-cable, or two-output routing. Unknown modes throw. */
	setHeadphoneOutputMode(mode: HeadphoneOutputMode): void;
	/** Enumerate browser audio-output devices for explicit sink selection. */
	refreshHeadphoneOutputs(): Promise<void>;
	/** Acquire an output through the browser's user-gesture permission chooser and select it. */
	acquireHeadphoneOutput(): Promise<void>;
	/** Route the real monitor element to an explicitly enumerated output device. */
	selectHeadphoneOutput(deviceId: string): Promise<void>;
	/** Pin the room mix to an enumerated output via AudioContext.setSinkId. */
	selectMasterOutput(deviceId: string): Promise<void>;
	/** Pin label-unlock / capture to an enumerated input. Never default a headphone mic. */
	selectAudioInput(deviceId: string): Promise<void>;
}
