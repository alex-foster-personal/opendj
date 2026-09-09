/**
 * The audio engine's public method contract.
 *
 * Split out of the former lib/rb/types.ts god module: it is the one entry
 * there that is a behavioral contract rather than a data shape, and it sits
 * downstream of every other module in this group.
 */

import type { DeckId } from './deck-slots';
import type { DeckAudioSnapshot, QuantizeGrid, SyncMode } from './deck-state-types';
import type { CrossfaderAssign, EqBand } from './mixer-types';
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
	load(deck: DeckId, stable_id: string): Promise<void>;
	/** Start/resume transport from the current position. Throws if no track
	 * is loaded on the deck. */
	play(deck: DeckId): Promise<void>;
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
	setDeckMaster(deck: DeckId): Promise<void>;
	/** Mute one real stem group. Rejects unless aligned artifacts are ready. */
	setStemMute(deck: DeckId, stem: StemControl, muted: boolean): void;
	/** Solo one real stem group. Rejects unless aligned artifacts are ready. */
	setStemSolo(deck: DeckId, stem: StemControl, solo: boolean): void;
	/** Explicit seek entry point. cueJump delegates here so quantize is central. */
	quantizedSeek(deck: DeckId, ms: number): Promise<void>;
	/** DECKUX-09: defer a hot-cue jump to `armAtPositionSec` on the deck's own
	 * transport clock; returns the absolute AudioContext time it lands at. */
	armHotCueTrigger(deck: DeckId, targetPositionMs: number, armAtPositionSec: number): Promise<number>;
	/** The engine's AudioContext clock, for projecting an armed trigger's
	 * remaining wait without exposing the context itself. */
	contextTimeNowSec(): number;
	/** Capture real post-Signalsmith analyser data; never synthesised. */
	captureDeckAudio(deck: DeckId): DeckAudioSnapshot;
	/** TRIM knob 0..1 (0.5 = unity) -> per-channel input GainNode. */
	setTrim(deck: DeckId, value: number): void;
	/** One EQ band knob 0..1 (0.5 = flat) -> Biquad gain in dB. */
	setEq(deck: DeckId, band: EqBand, value: number): void;
	/** FILTER knob 0..1 (0.5 = bypass) -> paired lowpass/highpass Biquad
	 * corner frequencies. See player/constants.ts FILTER_*. */
	setFilter(deck: DeckId, value: number): void;
	/** Channel fader 0..1 -> fader GainNode. */
	setFader(deck: DeckId, value: number): void;
	/** Crossfader 0..1 (0 = full A, 1 = full B); applies the gain pair to
	 * every channel assigned A or B. */
	setCrossfader(value: number): void;
	/** Route a channel to crossfader bus A, B, or THRU (bypass). */
	assignChannel(deck: DeckId, assign: CrossfaderAssign): void;
	/** Enable or disable a channel's post-EQ, pre-fader headphone cue tap. */
	setChannelCue(deck: DeckId, enabled: boolean): void;
	/** Set CUE-to-MASTER monitor mix and headphone level. */
	setHeadphoneMix(value: number): void;
	setHeadphoneLevel(value: number): void;
	/** Enumerate browser audio-output devices for explicit sink selection. */
	refreshHeadphoneOutputs(): Promise<void>;
	/** Acquire an output through the browser's user-gesture permission chooser and select it. */
	acquireHeadphoneOutput(): Promise<void>;
	/** Route the real monitor element to an explicitly enumerated output device. */
	selectHeadphoneOutput(deviceId: string): Promise<void>;
}
