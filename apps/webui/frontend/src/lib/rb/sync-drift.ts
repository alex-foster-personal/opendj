/**
 * Beat Sync drift measurement, re-enable decisions and the greppable sync log
 * lines (SYNCLOG-9650).
 *
 * the maintainer, Wed 4 Nov 2026: "beat sync when turned off and on isn't resnapping the
 * four beats properly - see current playing when it's gone out of sync. out of
 * sync moments should have easy flag to find in logs."
 *
 * Two log tags, each a single line that starts with the tag so
 * `grep -h SYNC_ ~/Library/Application\ Support/com.opendj.desktop/logs/*`
 * finds every one of them:
 *
 * - `SYNC_DRIFT`: a playing Beat Sync deck is off the master's phase by more
 *   than `SYNC_DRIFT_THRESHOLD_MS`, or (BAR mode, trusted bar numbers) its
 *   downbeat sits on a different beat of the bar than the master's. Logged
 *   once the drift has held for `SYNC_DRIFT_CONFIRM_MS` (`state=start`), again
 *   every `SYNC_DRIFT_RELOG_MS` while it lasts (`state=ongoing`), and once
 *   when it clears (`state=end`).
 * - `SYNC_RESNAP`: every Beat Sync re-enable: which decks were snapped to
 *   which master, the drift before the snap, or why nothing was snapped.
 *
 * The phase lock (phase-lock.ts) measures beat phase only, wrapped to half a
 * beat, so a whole-beat bar slip is invisible to it; this module measures the
 * bar too. Pure: no `$lib` state, the engines pass views.
 */
import type { AnlzBeat, AnlzBeatgrid } from '$lib/rb/anlz-types';
import { lockBeatPosition } from '$lib/rb/phase-lock';

export const SYNC_DRIFT_TAG = 'SYNC_DRIFT';
export const SYNC_RESNAP_TAG = 'SYNC_RESNAP';
export type SyncLogTag = typeof SYNC_DRIFT_TAG | typeof SYNC_RESNAP_TAG;

/** A flam starts near 10 ms; past this two decks are heard as two hits. */
export const SYNC_DRIFT_THRESHOLD_MS = 10;
/** Drift must hold this long before it is logged, so feed jitter and the
 * phase lock's own short corrections never write a line. */
export const SYNC_DRIFT_CONFIRM_MS = 500;
/** While a drift lasts it is logged again this often. */
export const SYNC_DRIFT_RELOG_MS = 10_000;
/** How often the engines check drift (4 Hz is plenty for a log). */
export const SYNC_DRIFT_CHECK_INTERVAL_MS = 250;

/** One deck as the drift measure needs it, both read at the same instant. */
export interface SyncDeckView {
	beats: readonly AnlzBeat[];
	positionSec: number;
	/** The deck's playback tempo ratio. */
	tempo: number;
}

export interface SyncDriftMeasure {
	/** Signed beat-phase error, wall-clock ms; positive = follower ahead. */
	driftMs: number;
	/** The same error in master beats, in [-0.5, 0.5). */
	driftBeats: number;
	/** Whole beats the follower's bar sits off the master's, in -1..2; null
	 * when bar numbers are not comparable (folded tempo or untrusted bars). */
	barSlipBeats: number | null;
	/** Beat-in-bar plus fraction, 1.00 up to 4.99 (3.50 = halfway through beat 3). */
	masterBarPhase: number;
	followerBarPhase: number;
	/** Audible BPM: local grid BPM times the tempo ratio. */
	masterBpm: number;
	followerBpm: number;
}

const _BPM_HALF_WINDOW_BEATS = 4;

function _localBpm(beats: readonly AnlzBeat[], index: number): number {
	const last = beats.length - 1;
	const from = Math.max(0, Math.floor(index) - _BPM_HALF_WINDOW_BEATS);
	const to = Math.min(last, Math.floor(index) + 1 + _BPM_HALF_WINDOW_BEATS);
	return (60 * (to - from)) / (beats[to].t - beats[from].t);
}

function _barPhase(beats: readonly AnlzBeat[], index: number): number {
	const i = Math.floor(index);
	return beats[i].n + (index - i);
}

function _wrapBarSlip(wholeBeats: number): number {
	const slip = ((wholeBeats % 4) + 4) % 4;
	return slip > 2 ? slip - 4 : slip;
}

/**
 * How far `follower` is off `master`, measured through each deck's own grid
 * as the phase lock reads it. Null when either deck is off its grid (before
 * the first beat or past the last), where there is no phase to compare.
 */
export function measureSyncDrift(
	master: SyncDeckView,
	follower: SyncDeckView,
	options: { normalization?: 0.5 | 1 | 2; barPhaseTrusted: boolean }
): SyncDriftMeasure | null {
	if (!(master.tempo > 0) || !(follower.tempo > 0)) {
		throw new RangeError(
			`sync drift: tempo ratios must be positive, got ${master.tempo} and ${follower.tempo}`
		);
	}
	const m = lockBeatPosition(master.beats, master.positionSec);
	const f = lockBeatPosition(follower.beats, follower.positionSec);
	if (m === null || f === null) return null;
	const normalization = options.normalization ?? 1;
	const mi = Math.floor(m);
	const beatWallMs = ((master.beats[mi + 1].t - master.beats[mi].t) / master.tempo) * 1000;
	const diff = f / normalization - m;
	const driftBeats = diff - Math.round(diff);
	const masterBarPhase = _barPhase(master.beats, m);
	const followerBarPhase = _barPhase(follower.beats, f);
	const barSlipBeats =
		normalization === 1 && options.barPhaseTrusted
			? _wrapBarSlip(Math.round(followerBarPhase - masterBarPhase - driftBeats))
			: null;
	return {
		driftMs: driftBeats * beatWallMs,
		driftBeats,
		barSlipBeats,
		masterBarPhase,
		followerBarPhase,
		masterBpm: _localBpm(master.beats, m) * master.tempo,
		followerBpm: _localBpm(follower.beats, f) * follower.tempo
	};
}

/** Bar numbers (`n`) are comparable only when no part of the grid is flagged
 * bar-phase untrusted, block-wide or on any beat. */
export function gridBarPhaseTrusted(
	beatgrid: Pick<AnlzBeatgrid, 'bar_phase_untrusted' | 'beats'> | undefined
): boolean {
	return (
		beatgrid !== undefined &&
		beatgrid.bar_phase_untrusted !== true &&
		!beatgrid.beats.some((beat) => beat.bar_phase_untrusted === true)
	);
}

/** Out of sync: off the beat past the flam line, or (BAR) on the wrong beat of the bar. */
export function isSyncDrifting(measure: SyncDriftMeasure, mode: 'beat' | 'bar'): boolean {
	if (Math.abs(measure.driftMs) > SYNC_DRIFT_THRESHOLD_MS) return true;
	return mode === 'bar' && measure.barSlipBeats !== null && measure.barSlipBeats !== 0;
}

// ----------------------------------------------------------- drift episodes

export type SyncDriftLogState = 'start' | 'ongoing' | 'end';

interface _Episode {
	sinceMs: number | null;
	loggedAtMs: number | null;
}

/** Per-deck drift episodes: which observation earns a log line. */
export interface SyncDriftMonitor {
	/** Feed one check; returns the line state to log now, or null. */
	observe(key: number, drifting: boolean, nowMs: number): SyncDriftLogState | null;
	/** The deck stopped being a follower (paused, unloaded, sync off): no `end` line. */
	forget(key: number): void;
}

export function createSyncDriftMonitor(): SyncDriftMonitor {
	const episodes = new Map<number, _Episode>();
	return {
		observe(key, drifting, nowMs) {
			const ep = episodes.get(key) ?? { sinceMs: null, loggedAtMs: null };
			episodes.set(key, ep);
			if (!drifting) {
				const logged = ep.loggedAtMs !== null;
				ep.sinceMs = null;
				ep.loggedAtMs = null;
				return logged ? 'end' : null;
			}
			ep.sinceMs ??= nowMs;
			if (ep.loggedAtMs === null) {
				if (nowMs - ep.sinceMs < SYNC_DRIFT_CONFIRM_MS) return null;
				ep.loggedAtMs = nowMs;
				return 'start';
			}
			if (nowMs - ep.loggedAtMs < SYNC_DRIFT_RELOG_MS) return null;
			ep.loggedAtMs = nowMs;
			return 'ongoing';
		},
		forget(key) {
			episodes.delete(key);
		}
	};
}

// --------------------------------------------------------- re-enable policy

export type BeatSyncReenableAction<D extends number = number> =
	/** Snap this deck's tempo and bar phase to the playing master. */
	| { kind: 'join-master'; master: D }
	/** This deck IS the master: snap every playing synced follower to it. */
	| { kind: 'resnap-followers' }
	/** The master's grid is untrusted (even when this deck is the master): nothing to snap to; say so. */
	| { kind: 'skip'; reason: 'master_grid_untrusted' }
	/** No playing master to snap to. */
	| { kind: 'skip'; reason: 'no_playing_master' };

/**
 * What a Beat Sync OFF -> ON press does on a playing deck with a trusted grid.
 *
 * Before SYNCLOG-9650 the master case and the untrusted-master case both returned
 * silently with SYNC lit, so a deck that drifted while sync was off (or a
 * group whose bar had slipped) stayed out of sync after the press.
 */
export function beatSyncReenableAction<D extends number>(input: {
	deck: D;
	playingMaster: D | null;
	masterGridTrusted: boolean;
}): BeatSyncReenableAction<D> {
	const { deck, playingMaster } = input;
	if (playingMaster === null) return { kind: 'skip', reason: 'no_playing_master' };
	// Trust first, the master's own press included: an untrusted master runs
	// the mix without Beat Sync (PLAY-28), so nothing may enter the grid path.
	if (!input.masterGridTrusted) return { kind: 'skip', reason: 'master_grid_untrusted' };
	if (playingMaster === deck) return { kind: 'resnap-followers' };
	return { kind: 'join-master', master: playingMaster };
}

// --------------------------------------------------------------- log lines

export type SyncLogValue = string | number | boolean | null;

function _signed(value: number, digits: number): string {
	const text = value.toFixed(digits);
	return value >= 0 && !text.startsWith('-') ? `+${text}` : text;
}

/** The measure as log fields, rounded for reading. */
export function syncDriftFields(measure: SyncDriftMeasure): Record<string, SyncLogValue> {
	return {
		drift_ms: _signed(measure.driftMs, 1),
		drift_beats: _signed(measure.driftBeats, 3),
		bar_slip_beats: measure.barSlipBeats,
		master_bar_phase: measure.masterBarPhase.toFixed(2),
		follower_bar_phase: measure.followerBarPhase.toFixed(2),
		master_bpm: measure.masterBpm.toFixed(2),
		follower_bpm: measure.followerBpm.toFixed(2)
	};
}

/** `TAG key=value ...`, one line, values without spaces (null prints `null`). */
export function formatSyncLogLine(tag: SyncLogTag, fields: Record<string, SyncLogValue>): string {
	const parts = Object.entries(fields).map(([key, value]) => {
		if (!/^[a-z_]+$/.test(key)) throw new RangeError(`sync log key must be snake_case, got ${key}`);
		const text = value === null ? 'null' : String(value);
		if (/\s/.test(text))
			throw new RangeError(`sync log value for ${key} must not contain whitespace: ${text}`);
		return `${key}=${text}`;
	});
	return [tag, ...parts].join(' ');
}
