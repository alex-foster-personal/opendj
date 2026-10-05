/**
 * Deck transport read model: everything one deck panel and its wavestack row
 * render, plus the loop and sync shapes that feed it. Owned by the
 * audio-engine rune store (MUST live in a .svelte.ts module).
 *
 * Split out of the former lib/rb/types.ts god module. This module composes
 * the payload contracts (anlz, stems, hot cues) into the state the UI reads;
 * it is deliberately downstream of them and nothing here is a wire shape.
 */

import type { AnlzData } from './anlz-types';
import type { DeckId } from './deck-slots';
import type { HotCue, HotCueSlot } from './hot-cue-types';
import type { StemDeckState } from './stem-types';

/** Beat Sync phase target. Beat mode matches the closest beat; bar mode also
 * requires the follower PQTZ beat number (1..4) to match the master. */
export type SyncMode = 'beat' | 'bar';

/** Quantize grid a quantized seek/cue/loop-end snaps to, in beats. 'phase'
 * (match to the detected phase length) is plumbed but NOT implemented
 * (pin a67bafbfc4b0) - selecting it is rejected with not_implemented before
 * it can reach the engine (performance-ipc.svelte.ts _dispatchUnknown). */
export type QuantizeGrid = 1 | 4 | 8 | 'phase';

/** Active loop on a deck. */
export interface LoopState {
	/** Loop-in position ms. */
	in_ms: number;
	/** Loop-out position ms. */
	out_ms: number;
	/** True when the audio engine is actively looping this range. */
	engaged: boolean;
	/** Beat length (4/8/16) when beat-quantised; null when time-based. */
	beat_length: number | null;
}

/** One secondary in-deck safety loop (auto-engages when its out is reached). */
export interface SafetyLoopSlot {
	in_ms: number;
	out_ms: number;
	beat_length: number | null;
	/** When true, engine engages this loop when playback reaches its out. */
	armed: boolean;
}

/** Everything one deck panel + its wavestack row renders. Owned by the
 * audio-engine rune store (MUST live in a .svelte.ts module). */
export interface DeckState {
	/** Which physical deck this is. */
	deck_id: DeckId;
	/** Loaded track stable id; null = empty deck (blank wave row, dim panel). */
	stable_id: string | null;
	/** Resolved source path from TrackOut.file_path at load time; null when unknown. */
	source_path: string | null;
	/** Track title; null until loaded. */
	title: string | null;
	/** Track artist; null until loaded. */
	artist: string | null;
	/** Track rating (0-5, library convention); null until loaded or unrated.
	 * Editable in-place from the deck header, same PATCH path as the
	 * library rating cell (see audio-engine.svelte.ts rateDeckTrack). */
	rating: number | null;
	/** Track BPM from TrackOut (format to 2dp, e.g. 128.00); null unknown. */
	bpm: number | null;
	/** Camelot key text from TrackOut (e.g. '7A'); null unknown. */
	key: string | null;
	/** Musical pitch transposition applied by the real deck DSP, in integer
	 * semitones from -12 through +12. The source key remains immutable. */
	key_shift_semitones: number;
	/** Track length ms from TrackOut.duration_ms; null until loaded. */
	duration_ms: number | null;
	/** Playhead position ms - UI mirror of the engine clock, updated via rAF. */
	position_ms: number;
	/** Desired transport state. Future worklet schedules update this immediately. */
	playing: boolean;
	/** True only while the currently committed worklet segment is audible. */
	audible: boolean;
	/** True while desired transport/DSP state is scheduled but not yet audible. */
	transport_pending: boolean;
	/** CUE point ms for return-to-cue transport semantics; null = track start. */
	cue_ms: number | null;
	/** Playback rate ratio; 1.0 = 0% pitch. Driven live by the deck pitch
	 * fader (setTempoRatio), constrained to the deck's selected pitch range. */
	pitch: number;
	/** Quantize transport seeks, cue placement, and loop endpoints to PQTZ. */
	quantize_enabled: boolean;
	/** Quantize grid the seek/cue/loop-end snaps to (pin a67bafbfc4b0). */
	quantize_grid_beats: QuantizeGrid;
	/** Follow the elected master deck's local PQTZ tempo and phase. */
	beat_sync_enabled: boolean;
	/** Operator arm for KEY SYNC. Whether the deck is actually following a
	 * master right now is keySyncStatus(), which the control lights from. */
	key_sync_enabled: boolean;
	/** Preserve source pitch while tempo changes through Signalsmith Stretch. */
	master_tempo_enabled: boolean;
	/** Arm slip mode. This does not alter an existing transport schedule. */
	slip_enabled: boolean;
	/** True only while an audible loop has an independently advancing hidden transport position. */
	slip_active: boolean;
	/** Hidden linear playhead in milliseconds while SLIP is active, otherwise null. */
	slip_position_ms: number | null;
	/** Beat-only or beat-number-within-bar phase alignment. */
	sync_mode: SyncMode;
	/** Explicit grid/rate/scheduling failure. null means no sync failure. */
	sync_error: string | null;
	/** Terminal AudioWorklet failure. null means the processor is healthy. */
	processor_error: string | null;
	/** Real precomputed stem artifact and graph state. */
	stems: StemDeckState;
	/** Active loop or null. */
	loop: LoopState | null;
	/** Secondary safety loop (one slot); auto-engages at its out when armed. */
	safety_loop: SafetyLoopSlot | null;
	/** Hot-cue bank content (empty slots = letters absent from this array). */
	hot_cues: HotCue[];
	/** Server-issued CAS revisions for every A-H slot, including empty slots. */
	hot_cue_revisions: Record<HotCueSlot, string>;
	/** Track.has_rb_mapping for the loaded track. False means hot-cue SAVE
	 * would 404 (djmdCue is keyed by djmdContent.ID, which a locally
	 * imported track has none of) - the bank goes inert-with-tooltip rather
	 * than let the pad fire a write that cannot succeed (PARITY-TODO,
	 * issue #736). True on an empty deck: nothing is loaded to gate. */
	has_rb_mapping: boolean;
	/** Analysis payload once fetched; null while absent. */
	anlz: AnlzData | null;
	/** Explicit no-analysis state: the error code (e.g. ANALYSIS_NOT_FOUND)
	 * when /anlz 404s. Renders the 'no analysis' treatment - never invented
	 * waveforms. null = not attempted or succeeded. */
	anlz_error: string | null;
	/** Last successful load wall time in ms; null until a load completes.
	 * UI shows a rounded form (e.g. 2.3s / ~300ms) - performance feature. */
	last_load_latency_ms: number | null;
	/** Monotonic successful-load commit number. Consumers use this durable edge
	 * to distinguish a completed reload from the outgoing track it replaced.
	 * Survives an unload (engine.unload carries it forward), so it stays
	 * monotonic across a destructive replace, which ejects before it loads.
	 * Only a full engine dispose resets it. */
	load_generation: number;
	/** Last load stage timings (ms) for IPC/CLI KPI; null until a load. */
	last_load_stages: Record<string, number> | null;
	/** Globally exclusive MASTER deck state. */
	is_master: boolean;
}

/** Serializable post-deck-DSP, pre-mixer analyser snapshot.
 *  `time_domain` / `frequency_db` are current render-graph samples.
 *  `render_context_time_s` is AudioContext.currentTime at that sample.
 *  `presentation_context_time_s` is the last trusted
 *  getOutputTimestamp().contextTime (listener clock). Render may lead
 *  presentation; the two must not be collapsed into one field. */
export interface DeckAudioSnapshot {
	render_context_time_s: number;
	presentation_context_time_s: number;
	sample_rate_hz: number;
	fft_size: number;
	frequency_db: number[];
	time_domain: number[];
}
