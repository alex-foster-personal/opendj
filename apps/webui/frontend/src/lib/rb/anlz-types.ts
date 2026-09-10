/**
 * GET /api/v1/tracks/{stable_id}/anlz wire payload, mirrored 1:1
 * (COMPONENT-MAP 2.3). Empty arrays are REAL states (track has no cues / no
 * beatgrid) - never pad.
 *
 * Split out of the former lib/rb/types.ts god module: this module owns the
 * analysis contract only. Nothing here is a client read model.
 */

import type { HotCueSlot } from './hot-cue-types';

/** Cue taxonomy from the /anlz endpoint. Kind 9-11 rows are excluded server-side v1. */
export type AnlzCueKind = 'memory' | 'hot_cue' | 'loop';

/** One waveform band set. Arrays are same-length normalised floats 0..1. */
export interface AnlzWaveformBands {
	/** Number of points in each band array. */
	length: number;
	/** Low-frequency energy per point (rendered orange). */
	low: number[];
	/** Mid-frequency energy per point (rendered blue). */
	mid: number[];
	/** High-frequency energy per point (rendered white). */
	high: number[];
}

/** Waveform payload: 400-point preview strip + <=38400-point detail. */
export interface AnlzWaveform {
	/** 'tri' = real tri-band colour data (.EXT PWV5/PWV4); 'mono' = heights only
	 * (PWAV/PWV3) - when 'mono' the client renders a single-colour waveform,
	 * NEVER synthesised bands. */
	kind: 'tri' | 'mono';
	/** Native 400-point PWAV overview (deck strip + browser Preview column). */
	preview: AnlzWaveformBands;
	/** PWV3/PWV5 downsampled server-side via per-bucket max (wavestack rows). */
	detail: AnlzWaveformBands;
}

/** One beat from ANLZ PQTZ. */
export interface AnlzBeat {
	/** Beat number within the bar, 1..4. n == 1 is the bar tick. */
	n: number;
	/** Local BPM at this beat. */
	bpm: number;
	/** Beat time in SECONDS from track start. */
	t: number;
}

/** Which producer this beatgrid block came from. REQUIRED on every payload.
 *
 * The wire discriminator `hasTrustedBeatGrid` keys off. It is stated by BOTH
 * branches of the server's `build_anlz_payload`, never inferred from which
 * other fields happen to be present: a required field going missing or being
 * renamed must not be able to relabel an own payload as rekordbox and hand it
 * quantize, Beat Sync and beat loops by default. Consumers treat any value
 * other than these two as its own explicit untrusted branch. */
export type AnlzBeatgridSource = 'own' | 'rekordbox';

/** Lane status for an OWN beatgrid block (NATIVE-01). `missing` means no own
 * record has been written for this track yet (the backfill queue is the fix);
 * `failed` means the analyzer ran and could not measure, and carries the
 * lane's named reason. `ok` with an empty `beats` array is a contract
 * violation, not a state: the server refuses to serve one. */
export type AnlzBeatgridStatus = 'ok' | 'failed' | 'missing';

/** Beatgrid payload: ANLZ PQTZ when `source` is 'rekordbox', the own analysis
 * record when it is 'own'. */
export interface AnlzBeatgrid {
	/** Which producer served this block. Required; see AnlzBeatgridSource. */
	source: AnlzBeatgridSource;
	/** Total beats; always equal to beats.length. 0 with empty beats[] is a
	 * real no-grid state on a rekordbox payload. */
	beat_count: number;
	/** Ordered beats. */
	beats: AnlzBeat[];
	/** Own payloads only. A rekordbox-sourced block never carries it. */
	status?: AnlzBeatgridStatus;
	/** Own payloads only; the lane's named failure reason, null when ok. */
	reason?: string | null;
	/** Own payloads with `status: ok` only: the published ordinary-unit BPM
	 * this grid's beats project from, so the deck's public BPM cannot disagree
	 * with the grid it plays against. */
	bpm?: number;
	/** Own payloads with `status: ok` only. True when the analyzer detected a
	 * tempo change, so a single static grid is not trustworthy. ABSENT on a
	 * rekordbox payload, where absence means trusted. */
	static_grid_untrusted?: boolean;
}

/** One tempo change detected by the own beatgrid analyzer (NATIVE-03). */
export interface AnlzTempoChange {
	/** Time of the change in SECONDS from track start. */
	at_s: number;
	/** Mean BPM of the section before the change, in the published octave. */
	bpm_before: number;
	/** Mean BPM of the section after it, in the published octave. */
	bpm_after: number;
	/** Rank score for the changepoint, 0..1. Not a probability. */
	confidence: number;
}

/** One cue/loop row sourced from djmdCue (ANLZ PCOB is empty in rb6/7). */
export interface AnlzCue {
	/** memory | hot_cue | loop (djmdCue Kind 0 / 1..8 / OutMsec > 0). */
	kind: AnlzCueKind;
	/** Hot-cue bank letter A..H; null for memory cues. */
	slot: HotCueSlot | null;
	/** Cue-in position in MILLISECONDS. */
	in_ms: number;
	/** Loop-out position in ms; null when not a loop. */
	out_ms: number | null;
	/** True when this row defines a loop (out_ms present). */
	is_loop: boolean;
	/** True when rekordbox stored this loop as the active one. */
	active_loop: boolean;
	/** Vendor loop-size metadata. Values originating in djmdCue can be packed;
	 * do not display this as a beat count without validating its encoding. */
	beat_loop_size: number | null;
	/** rekordbox colour table index for the marker; null when unset. */
	color_table_index: number | null;
	/** User comment on the cue ('2nd drop'); null when unset. */
	comment: string | null;
}

/** One phrase from ANLZ .EXT PSSI; response field may be an empty array. */
export interface AnlzPhrase {
	/** Phrase start in seconds. */
	start_s: number;
	/** Phrase end in seconds. */
	end_s: number;
	/** Raw PSSI phrase kind id (rendered as chevron cadence only at v1). */
	kind: number;
	/** Raw PSSI mood id. */
	mood: number;
}

/** Optional analyzer hints for future dynamic key and tempo-aware sync. */
export interface AnlzPerformanceHints {
	/** Key changes detected across the track; absent until a real analyzer emits them. */
	dynamic_key?: boolean;
	/** Tempo shifts detected across the track; absent until a real analyzer emits them. */
	dynamic_tempo?: boolean;
	/** Detected musical mode, e.g. major or minor; absent until truly analyzed. */
	musical_mode?: 'major' | 'minor';
	/** Named harmonic progression, e.g. I-V-vi-IV; absent until truly analyzed. */
	chord_progression?: string;
}

/** Our own ffmpeg-decoded waveform status for a track with NO rekordbox
 * mapping (issue #735). Present ONLY on that branch - a rekordbox-mapped
 * track's response has no ``local_waveform`` key at all. */
export interface AnlzLocalWaveform {
	/** 'decoded' once our ffmpeg path produced peaks; 'not_decoded' names why
	 * in `reason` (no ffmpeg, no audio, saturated decoder, etc) - never a
	 * synthesised shape. */
	status: 'decoded' | 'not_decoded';
	reason: string | null;
	/** 120x3-band browser strip, same 360-byte contract as a listing row's
	 * preview_b64 - present only once status is 'decoded'. */
	preview_b64: string | null;
	preview_max: number | null;
	/** True only on a 'not_decoded' caused by a TRANSIENT condition (decoder
	 * momentarily saturated) - the caller should retry, and the response is
	 * sent with `Cache-Control: no-store` so it never sticks. Absent/false
	 * means a fact about the track itself (no ffmpeg, corrupt audio, missing
	 * file); retrying will not help. */
	retryable?: boolean;
}

/** Full GET /tracks/{stable_id}/anlz response. */
export interface AnlzData {
	/** 40-hex stable id the payload belongs to. */
	stable_id: string;
	/** The points param the detail waveform was downsampled to (<=38400). */
	points: number;
	/** Waveform bands; see AnlzWaveform. */
	waveform: AnlzWaveform;
	/** Beatgrid; see AnlzBeatgrid. */
	beatgrid: AnlzBeatgrid;
	/** Cues + loops; sparse coverage is real - most tracks show none. */
	cues: AnlzCue[];
	/** Phrases; empty array when PSSI absent. */
	phrases: AnlzPhrase[];
	/** Own beatgrid payloads only: time-stamped tempo-change markers, empty
	 * when the analyzer found none. A rekordbox-sourced payload has no such
	 * key at all. */
	tempo_changes?: AnlzTempoChange[];
	/** Optional, real analyzer-only detail. Absence means not analyzed. */
	performance_hints?: AnlzPerformanceHints;
	/** See AnlzLocalWaveform; absent for a rekordbox-mapped track. */
	local_waveform?: AnlzLocalWaveform;
}
