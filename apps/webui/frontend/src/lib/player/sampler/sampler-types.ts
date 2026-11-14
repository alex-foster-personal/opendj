/**
 * Edge sampler v1: shared contracts between server, engine, UI and agent parity.
 *
 * Wire shapes mirror `apps/webui/server/routes/sampler.py` and `apps/webui/server/sampler_models.py`
 * (snake_case on the wire). Hand-written: the sampler routes are not in the generated api-types.ts yet.
 * Spec: `(private path)` sections 3.2 (slot, vector), 7.4 (UI), 10 (parity).
 * UI contract: `(private path)` (mock v12).
 */

import type { Repeat } from './sampler-repeat';

export type SlotSource = 'loop_hot_cue' | 'memory_loop' | 'capture' | 'starter';
/** `sort` of GET /slots (sampler_core.SlotSort). */
export type SlotSort = 'compat' | 'track' | 'recent';
export type LaunchQuant = 'bar' | 'beat' | '4bars' | 'off';
export type KeyMode = 'auto' | 'master_tempo' | 'off';
export type TempoMode = 'match' | 'own';
export type PlayMode = 'loop' | 'one_shot' | 'gate';

/** Captured deck mix state, applied at play time (never baked into audio). */
export interface SamplerVector {
	trim_db: number;
	eq_db: { low: number; mid: number; high: number };
	/** -1..1, 0 = off; negative = low-pass, positive = high-pass (the deck colour filter) */
	filter: number;
	stems: Record<
		'vocals' | 'drums' | 'bass' | 'inst',
		{ gain_db: number; mute: boolean; solo: boolean }
	> | null;
	key_shift_st: number;
	master_tempo: boolean;
}

export interface SlotSettings {
	launch: LaunchQuant;
	key: KeyMode;
	tempo: TempoMode;
	mode: PlayMode;
	repeat: Repeat;
	/** whole beats, -3..3 */
	downbeat_offset: number;
	/** 0..100 */
	level: number;
}

/** 3-band peaks across exactly the slot's region, 0..1, same length per band. */
export interface RegionPeaks {
	low: number[];
	mid: number[];
	high: number[];
}

export interface SlotFlags {
	broken: boolean;
	remix: boolean;
	vocals: boolean;
	offline: boolean;
}

export interface SamplerSlot {
	slot_id: string;
	stable_id: string;
	source: SlotSource;
	cue_letter: string | null;
	in_ms: number;
	out_ms: number;
	beats: number;
	native_bpm: number;
	/** Camelot, e.g. "8A"; null when the track has no key */
	key: string | null;
	energy: number | null;
	/** CSS colour for the edge strip (the hot cue colour) */
	color: string;
	title: string;
	artist: string;
	/** user rename; null = show `title` */
	name: string | null;
	description: string;
	vector: SamplerVector | null;
	settings: SlotSettings;
	flags: SlotFlags;
	peaks: RegionPeaks;
	/** the track has a stem-set, so stem settings can play (EDGESAMP-11872h) */
	stems_ready: boolean;
	/** the stem-set's parts as vector part names; null when not stems_ready */
	stem_parts: Array<'vocals' | 'drums' | 'bass' | 'inst'> | null;
}

export interface SlotsPage {
	slots: SamplerSlot[];
	total: number;
	skipped_no_grid: number;
	/** whether rekordbox loop cues were read: "ok" or "unavailable: <reason>" */
	rekordbox: string;
}

export interface SetMeta {
	id: string;
	name: string;
	description: string;
	/** user-owned layout (after a drop or reorder); null = computed from ranking */
	slot_ids: Array<string | null> | null;
}

export interface CaptureRequest {
	stable_id: string;
	in_ms: number;
	out_ms: number;
	beats: number;
	vector: SamplerVector;
	set_id: string;
	index: number;
}

export type Edge = 'L' | 'R';

/** Engine-side voice state, read by the UI and published on the ui-mirror. */
export interface VoiceState {
	voice: 5 | 6 | 7 | 8;
	slot_id: string | null;
	phase: 'free' | 'armed' | 'playing';
	/** launched with no master playing: native tempo, no quantise (the deck's FREE) */
	free: boolean;
	/** AudioContext seconds the region starts (or started) */
	start_ctx_sec: number | null;
	/** seconds per loop pass at the current rate, for the playhead */
	pass_sec: number | null;
	rate: number;
	semitones: number;
	/** the slot plays its mix clip because its stems are pending (EDGESAMP-11872h) */
	stems_pending: boolean;
}

export const DEFAULT_SETTINGS: SlotSettings = {
	launch: 'bar',
	key: 'auto',
	tempo: 'match',
	mode: 'loop',
	repeat: 'loop',
	downbeat_offset: 0,
	level: 80
};

/** The name a tab shows: the user's rename, else the track title. */
export const displayName = (s: Pick<SamplerSlot, 'name' | 'title'>): string => s.name || s.title;
/** Whole bars in a slot's region, at least 1. */
export const regionBars = (s: Pick<SamplerSlot, 'beats'>): number =>
	Math.max(1, Math.round(s.beats / 4));
