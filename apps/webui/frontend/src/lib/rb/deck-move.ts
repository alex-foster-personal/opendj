/**
 * DECKMOVE-01 deck move (the maintainer, Wed 4 Nov 2026): right-click a deck's artwork,
 * "Move track to deck N", and deck N takes over the same audio, so a repeating
 * bassline on deck 1 can carry on from deck 4.
 *
 * Industry reference: rekordbox / Serato "instant doubles", which clone the
 * track and the playhead onto another deck. This extends it with the engaged
 * loop (identical in/out, unquantized), the deck transport settings (tempo,
 * pitch range, key lock, key shift and KEY SYNC per KEYSYNC-01, quantize, sync,
 * slip), the channel strip (TRIM, EQ, FILTER / COLOR FX, stem EQ mode), the
 * stem controls and, for Move, a handover crossfade from the source channel to
 * the target channel after which the source stops and unloads.
 *
 * Timing: the target starts on the AUDIO clock at the exact source position
 * for its start instant (`startAlignedTo` projects the source's own schedule),
 * never from the rAF-published `position_ms`. The handover is automation on
 * the two fader gain params from that same instant (`scheduleHandover`).
 *
 * Gain law: the two decks play the SAME, sample-aligned audio, which sums in
 * amplitude. A cos/sin equal-power pair assumes uncorrelated sources and gives
 * +3 dB at the midpoint on a correlated pair, so the law here is its square,
 * cos^2 / sin^2: the amplitudes sum to exactly 1 at every instant (no bump, no
 * dip), with the same smooth S-shaped ends. Hygiene mimicked from the
 * coherent-crossfade rule; no taste calibration.
 *
 * Channel fader / crossfader side of the target: Move copies the source's
 * crossfader assignment and ends the target fader at the source's fader level,
 * because otherwise the target can be silent (other crossfader side) and the
 * move would cut the audio. Copy (instant doubles) leaves the target's fader
 * and assignment as the operator set them, the way rekordbox does.
 *
 * Pure orchestration over a port: the engine binding lives in
 * deck-move-port.svelte.ts so the artwork menu, HTTP, `opendj deck_move` and
 * MCP `deck_move` share this one function.
 *
 * -Claude
 */
import type { DeckId } from './deck-id';
import type { DeckState, QuantizeGrid, SyncMode } from './deck-state-types';
import type { CrossfaderAssign, EqBand, MixerChannelState } from './mixer-types';
import type { StemControl, StemControlState } from './stem-types';
import type { PitchRange } from '../player/constants';
import type { ColorFxSettings } from './color-fx';
import type { KeySyncMode } from '../player/key/camelot';

// ----- configuration ------------------------------------------------------
export const DECK_MOVE_DEFAULT_CROSSFADE_BEATS = 8;
export const DECK_MOVE_MAX_CROSSFADE_BEATS = 64;
/** Samples in each automation curve: plenty for a smooth S at any length. */
export const HANDOVER_CURVE_POINTS = 256;
/** Cleanup waits this long past the curve's end on the audio clock. */
export const HANDOVER_SETTLE_SEC = 0.05;
const NEUTRAL_STEM: StemControlState = { muted: false, solo: false, gain: 0.5 };
const EQ_BANDS: readonly EqBand[] = ['low', 'mid', 'high'];

export type DeckMoveMode = 'move' | 'copy';

export interface DeckMoveRequest {
	from: DeckId;
	to: DeckId;
	crossfade_beats: number;
	mode: DeckMoveMode;
	/** Move onto a deck that is playing anyway (its track is ejected first). */
	force?: boolean;
}

export type DeckMoveRefusalCode = 'same_deck' | 'source_not_loaded' | 'target_playing' | 'bad_crossfade' | 'no_tempo';

export class DeckMoveRefused extends Error {
	readonly code: DeckMoveRefusalCode;
	constructor(code: DeckMoveRefusalCode, message: string) {
		super(message);
		this.name = 'DeckMoveRefused';
		this.code = code;
	}
}

export type DeckMoveLoopOutcome = 'engaged' | 'stored_not_engaged' | 'none';

export interface DeckMoveResult {
	from: DeckId;
	to: DeckId;
	mode: DeckMoveMode;
	stable_id: string;
	/** AudioContext second the target started (null when the source was paused). */
	start_context_sec: number | null;
	crossfade_sec: number;
	loop: DeckMoveLoopOutcome;
	stems_copied: StemControl[];
	/** The track the target held before (ejected by the move), or null. */
	replaced_stable_id: string | null;
}

type DeckView = Pick<
	DeckState,
	| 'stable_id' | 'duration_ms' | 'position_ms' | 'playing' | 'bpm' | 'pitch' | 'key_shift_semitones'
	| 'master_tempo_enabled' | 'key_sync_enabled' | 'key_sync_mode' | 'key_sync_target' | 'quantize_enabled'
	| 'quantize_grid_beats' | 'beat_sync_enabled' | 'sync_mode' | 'slip_enabled' | 'loop' | 'is_master'
> & { stems: Pick<DeckState['stems'], 'status' | 'controls' | 'available_controls'> };

/** What the orchestration needs from the engine. Every method is the real engine's own. */
export interface DeckMovePort {
	deck(deck: DeckId): DeckView;
	channel(deck: DeckId): MixerChannelState;
	colorFx(deck: DeckId): ColorFxSettings;
	pitchRange(deck: DeckId): PitchRange;
	masterLocked(): boolean;
	load(deck: DeckId, stableId: string): Promise<void>;
	unload(deck: DeckId): Promise<void>;
	pause(deck: DeckId): Promise<void>;
	setPitchRange(deck: DeckId, range: PitchRange): void;
	setTempoRatio(deck: DeckId, ratio: number): Promise<void>;
	setMasterTempo(deck: DeckId, enabled: boolean): Promise<void>;
	/** KEYSYNC-01 absolute key shift (key lock on, KEY SYNC disarmed). */
	setKeyShift(deck: DeckId, semitones: number): Promise<void>;
	setKeySync(deck: DeckId, enabled: boolean, options: { mode: KeySyncMode; target: DeckId | 'master' }): Promise<void>;
	setQuantize(deck: DeckId, enabled: boolean): void;
	setQuantizeGrid(deck: DeckId, grid: QuantizeGrid): void;
	setSyncMode(deck: DeckId, mode: SyncMode): Promise<void>;
	setBeatSync(deck: DeckId, enabled: boolean): Promise<void>;
	setSlip(deck: DeckId, enabled: boolean): Promise<void>;
	setLoop(deck: DeckId, loop: { in_ms: number; out_ms: number }, quantize: boolean): Promise<void>;
	setTrim(deck: DeckId, value: number): void;
	setEq(deck: DeckId, band: EqBand, value: number): void;
	setFilter(deck: DeckId, value: number): void;
	applyColorFx(deck: DeckId, settings: ColorFxSettings): void;
	setStemEqMode(deck: DeckId, enabled: boolean): void;
	waitStemsReady(deck: DeckId): Promise<void>;
	setStemMute(deck: DeckId, stem: StemControl, muted: boolean): void;
	setStemSolo(deck: DeckId, stem: StemControl, solo: boolean): void;
	setStemGain(deck: DeckId, stem: StemControl, value: number): void;
	setFader(deck: DeckId, value: number): void;
	assignChannel(deck: DeckId, assign: CrossfaderAssign): void;
	/** Paused park at an exact ms, no grid snap. */
	seekPaused(deck: DeckId, ms: number): Promise<void>;
	/** Start `deck` on the audio clock at `source`'s own position for that instant; returns the instant. */
	startAlignedTo(deck: DeckId, source: DeckId): Promise<number>;
	/** Fader automation on both channels from `startSec` (or just ahead of now if that passed): source level -> 0,
	 * target 0 -> level. Returns the instant it starts. */
	scheduleHandover(from: DeckId, to: DeckId, startSec: number, durationSec: number, level: number): number;
	waitUntilContextSec(sec: number): Promise<void>;
	setDeckMaster(deck: DeckId, options: { lock: boolean }): Promise<void>;
}

export interface DeckClone {
	stable_id: string;
	position_ms: number;
	playing: boolean;
	bpm: number | null;
	pitch: number;
	pitch_range: PitchRange;
	key_shift_semitones: number;
	master_tempo_enabled: boolean;
	key_sync_enabled: boolean;
	key_sync_mode: KeySyncMode;
	key_sync_target: DeckId | null;
	quantize_enabled: boolean;
	quantize_grid_beats: QuantizeGrid;
	beat_sync_enabled: boolean;
	sync_mode: SyncMode;
	slip_enabled: boolean;
	loop: { in_ms: number; out_ms: number; engaged: boolean } | null;
	is_master: boolean;
	stems: Partial<Record<StemControl, StemControlState>>;
	channel: MixerChannelState;
	color_fx: ColorFxSettings;
}

// ----- pure helpers -------------------------------------------------------
/** Gains at handover progress t in [0, 1]: amplitudes summing to 1 (coherent, no bump). */
export function handoverGains(t: number): { source: number; target: number } {
	const clamped = Math.min(1, Math.max(0, t));
	const target = Math.sin((Math.PI / 2) * clamped) ** 2;
	return { source: 1 - target, target };
}

/** The two fader automation curves for a handover at fader `level`. */
export function handoverCurves(level: number, points = HANDOVER_CURVE_POINTS): { source: Float32Array; target: Float32Array } {
	if (!Number.isInteger(points) || points < 2) throw new RangeError(`curve needs at least 2 points, got ${points}`);
	const source = new Float32Array(points), target = new Float32Array(points);
	for (let i = 0; i < points; i += 1) {
		const gains = handoverGains(i / (points - 1));
		source[i] = level * gains.source;
		target[i] = level * gains.target;
	}
	return { source, target };
}

/** Seconds for `beats` at the deck's playing tempo (track BPM x pitch ratio). */
export function handoverDurationSec(beats: number, bpm: number | null, pitch: number): number {
	if (beats === 0) return 0;
	if (bpm === null || !(bpm > 0) || !(pitch > 0)) {
		throw new DeckMoveRefused('no_tempo', 'the track has no BPM, so a crossfade in beats has no length: send crossfade_beats 0 for a cut');
	}
	return (beats * 60) / (bpm * pitch);
}

function _stemDiffers(control: StemControlState): boolean {
	return control.muted !== NEUTRAL_STEM.muted || control.solo !== NEUTRAL_STEM.solo || control.gain !== NEUTRAL_STEM.gain;
}

/** A value snapshot of everything a move copies, taken at one instant. */
export function captureDeckClone(port: DeckMovePort, deck: DeckId): DeckClone {
	const st = port.deck(deck);
	if (st.stable_id === null) throw new DeckMoveRefused('source_not_loaded', `deck ${deck} has no track loaded`);
	const stems: Partial<Record<StemControl, StemControlState>> = {};
	if (st.stems.status === 'ready') {
		for (const stem of st.stems.available_controls) {
			const control = st.stems.controls[stem];
			if (_stemDiffers(control)) stems[stem] = { ...control };
		}
	}
	const loop = st.loop === null ? null : { in_ms: st.loop.in_ms, out_ms: st.loop.out_ms, engaged: st.loop.engaged };
	return {
		stable_id: st.stable_id,
		position_ms: st.position_ms,
		playing: st.playing,
		bpm: st.bpm,
		pitch: st.pitch,
		pitch_range: port.pitchRange(deck),
		key_shift_semitones: st.key_shift_semitones,
		master_tempo_enabled: st.master_tempo_enabled,
		key_sync_enabled: st.key_sync_enabled,
		key_sync_mode: st.key_sync_mode,
		key_sync_target: st.key_sync_target,
		quantize_enabled: st.quantize_enabled,
		quantize_grid_beats: st.quantize_grid_beats,
		beat_sync_enabled: st.beat_sync_enabled,
		sync_mode: st.sync_mode,
		slip_enabled: st.slip_enabled,
		loop,
		is_master: st.is_master,
		stems,
		channel: { ...port.channel(deck) },
		color_fx: { ...port.colorFx(deck) }
	};
}

/** Throws the reason a move must not run; returns when it may. Touches nothing. */
export function refuseDeckMove(port: DeckMovePort, request: DeckMoveRequest): void {
	const { from, to, crossfade_beats: beats } = request;
	if (from === to) throw new DeckMoveRefused('same_deck', `deck ${from} is already the source`);
	if (!Number.isFinite(beats) || beats < 0 || beats > DECK_MOVE_MAX_CROSSFADE_BEATS) {
		throw new DeckMoveRefused('bad_crossfade', `crossfade_beats must be 0..${DECK_MOVE_MAX_CROSSFADE_BEATS}, got ${beats}`);
	}
	const src = port.deck(from);
	if (src.stable_id === null || src.duration_ms === null) {
		throw new DeckMoveRefused('source_not_loaded', `deck ${from} has no track audio loaded to move`);
	}
	if (port.deck(to).playing && request.force !== true) {
		throw new DeckMoveRefused('target_playing', `deck ${to} is playing; send force: true to eject it and move anyway`);
	}
	if (request.mode === 'move' && src.playing) handoverDurationSec(beats, src.bpm, src.pitch);
}

export interface DeckMoveMenuEntry {
	to: DeckId;
	label: string;
	/** Picking an item that names a playing target is the explicit force. */
	force: boolean;
}

/** The artwork right-click items for `from`: "Move track to deck N" for every other deck, naming a held target. */
export function deckMoveMenuEntries(
	from: DeckId,
	decks: readonly DeckId[],
	view: (deck: DeckId) => { stable_id: string | null; title: string | null; playing: boolean }
): DeckMoveMenuEntry[] {
	return decks.filter((to) => to !== from).map((to) => {
		const target = view(to);
		const held = target.stable_id === null ? '' : ` (replaces ${target.title ?? 'its track'}${target.playing ? ', playing' : ''})`;
		return { to, label: `Move track to deck ${to}${held}`, force: target.playing };
	});
}

// ----- orchestration ------------------------------------------------------
async function _applyTransport(port: DeckMovePort, to: DeckId, clone: DeckClone): Promise<void> {
	port.setPitchRange(to, clone.pitch_range);
	await port.setTempoRatio(to, clone.pitch);
	await port.setMasterTempo(to, clone.master_tempo_enabled);
	// KEY SYNC derives the shift itself (same track, same leader); a manual shift is copied as a value.
	// A source that followed the target deck cannot hand that to it (a deck never follows itself): MASTER.
	const followed = clone.key_sync_target;
	const keyTarget = followed === null || followed === to ? 'master' : followed;
	if (!clone.key_sync_enabled && clone.key_shift_semitones !== port.deck(to).key_shift_semitones) {
		await port.setKeyShift(to, clone.key_shift_semitones);
	}
	await port.setKeySync(to, clone.key_sync_enabled, { mode: clone.key_sync_mode, target: keyTarget });
	port.setQuantize(to, clone.quantize_enabled);
	port.setQuantizeGrid(to, clone.quantize_grid_beats);
	await port.setSyncMode(to, clone.sync_mode);
	await port.setBeatSync(to, clone.beat_sync_enabled);
	await port.setSlip(to, clone.slip_enabled);
}

function _applyChannel(port: DeckMovePort, to: DeckId, clone: DeckClone): void {
	const ch = clone.channel;
	port.setTrim(to, ch.trim);
	for (const band of EQ_BANDS) port.setEq(to, band, ch[`eq_${band}`]);
	port.applyColorFx(to, clone.color_fx);
	port.setFilter(to, ch.filter);
	port.setStemEqMode(to, ch.stem_eq_mode);
}

async function _applyStems(port: DeckMovePort, to: DeckId, clone: DeckClone): Promise<StemControl[]> {
	const stems = Object.keys(clone.stems) as StemControl[];
	if (stems.length === 0) return [];
	await port.waitStemsReady(to);
	for (const stem of stems) {
		const control = clone.stems[stem] as StemControlState;
		port.setStemGain(to, stem, control.gain);
		port.setStemMute(to, stem, control.muted);
		port.setStemSolo(to, stem, control.solo);
	}
	return stems;
}

async function _applyLoop(port: DeckMovePort, to: DeckId, clone: DeckClone): Promise<DeckMoveLoopOutcome> {
	if (clone.loop === null) return 'none';
	if (!clone.loop.engaged) return 'stored_not_engaged';
	await port.setLoop(to, { in_ms: clone.loop.in_ms, out_ms: clone.loop.out_ms }, false);
	return 'engaged';
}

async function _retireSource(port: DeckMovePort, from: DeckId, to: DeckId, clone: DeckClone): Promise<void> {
	if (clone.is_master) await port.setDeckMaster(to, { lock: port.masterLocked() });
	port.setFader(to, clone.channel.fader);
	if (port.deck(from).playing) await port.pause(from);
	await port.unload(from);
	port.setFader(from, clone.channel.fader);
}

/** The one engine function behind the artwork menu, HTTP, `opendj deck_move` and MCP `deck_move`. */
export async function moveDeck(port: DeckMovePort, request: DeckMoveRequest): Promise<DeckMoveResult> {
	refuseDeckMove(port, request);
	const { from, to, mode } = request;
	const replaced = port.deck(to).stable_id;
	if (replaced !== null) await port.unload(to);
	const clone = captureDeckClone(port, from);
	await port.load(to, clone.stable_id);
	await _applyTransport(port, to, clone);
	_applyChannel(port, to, clone);
	const stemsCopied = await _applyStems(port, to, clone);
	const loop = await _applyLoop(port, to, clone);
	if (mode === 'move') {
		port.assignChannel(to, clone.channel.assign);
		port.setFader(to, 0);
	}
	const result: DeckMoveResult = {
		from, to, mode, stable_id: clone.stable_id, start_context_sec: null, crossfade_sec: 0, loop,
		stems_copied: stemsCopied, replaced_stable_id: replaced
	};
	if (!port.deck(from).playing) {
		await port.seekPaused(to, port.deck(from).position_ms);
		if (mode === 'move') await _retireSource(port, from, to, clone);
		return result;
	}
	result.start_context_sec = await port.startAlignedTo(to, from);
	if (mode === 'copy') return result;
	const src = port.deck(from);
	result.crossfade_sec = handoverDurationSec(request.crossfade_beats, src.bpm, src.pitch);
	const fadeAt = port.scheduleHandover(from, to, result.start_context_sec, result.crossfade_sec, clone.channel.fader);
	await port.waitUntilContextSec(fadeAt + result.crossfade_sec + HANDOVER_SETTLE_SEC);
	await _retireSource(port, from, to, clone);
	return result;
}
