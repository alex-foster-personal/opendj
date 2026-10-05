/**
 * Rust engine mode's page-decided commands: play, pause, CUE, seek, tempo,
 * Beat Sync, master and quantize, plus the hot-cue driver. Each is DECIDED on
 * the page with the Web Audio engine's own pure functions over our beatgrids
 * (`rust-sync.ts` names them) and the engine is sent the outcome: a tempo
 * ratio, a seek, a play. Loaded only with the mode (see `rust-mode.svelte.ts`).
 */
import { _hotCueRevisionsFrom, deckStates, mixerState, pitchRanges } from '$lib/player/state.svelte';
import { effectiveBeatSync, gridFeatureInertTip, gridFeaturesInert } from '$lib/player/grid-features';
import { fetchHotCueSlots } from '$lib/rb/api-rb';
import {
	assertPausedMasterSelectionAllowed,
	masterSwitchFollowers,
	pausedMasterSelectionBlockers
} from '$lib/rb/audio-engine-guards';
import { syncModeForBeatSyncMax } from '$lib/rb/beat-sync-decisions';
import { resolveArmAtPosition } from '$lib/rb/beat-sync-math';
import type { DeckState } from '$lib/rb/deck-state-types';
import { hotCuesFromAnlz } from '$lib/rb/hot-cue-from-anlz';
import { electMaster, runAutomaticRejoin } from '$lib/rb/master-election';
import type { PerformanceCommand, PerformanceHotCueDriver } from '$lib/rb/performance-ipc.svelte';
import { phaseLockDecision, phaseLockFeedForwardBase, phaseLockShouldSend } from '$lib/rb/phase-lock';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import type { EngineCommand } from './client';
import { phaseLocks, type PhaseLock } from './rust-phase-lock';

export { phaseLocksForTest, invalidateRustPhaseLocks } from './rust-phase-lock';
import { DECKS, type DeckId, displayLoops, loadFences, notify, playheadMs, send } from './rust-link';
import {
	electionInputFrom,
	lateJumpPositionMs,
	planRustFollowerJoin,
	reanchorNeedsSeek,
	rustCuePoint,
	rustSeekTarget,
	SYNC_LEAD_SEC,
	type SyncDeckView
} from './rust-sync';

/** The page's master, as the Web Audio engine keeps `_masterDeck` and
 * `_masterMode`. `is_master` on each deck mirrors `deck`. */
export const rustMaster: { deck: DeckId | null; mode: 'auto' | 'locked' } = {
	deck: null,
	mode: 'auto'
};

function _assignMaster(deck: DeckId | null): void {
	rustMaster.deck = deck;
	for (const d of DECKS) deckStates[d].is_master = d === deck;
}

/** The master that can drive Beat Sync phase: set and playing. */
function _syncMaster(): DeckId | null {
	const d = rustMaster.deck;
	return d !== null && deckStates[d].playing ? d : null;
}

export function electIfAuto(options?: { force?: boolean }): void {
	if (rustMaster.mode === 'locked' && !options?.force) return;
	if (options?.force) rustMaster.mode = 'auto';
	_assignMaster(electMaster(electionInputFrom(deckStates, mixerState)));
}

/**
 * An AUTOMATIC master change - the master paused, CUE'd, played out or was
 * unloaded - re-joins every playing synced deck to the new master, as the
 * manual `_setDeckMaster` does. Without it each follower's phase lock is
 * dropped as soon as the master moves (`_lockHolds`) and nothing records a
 * new one, so the decks free-run and drift. A re-anchor join only seeks a
 * follower that is off phase; the new master was itself locked to the old
 * one, so this is normally a tempo-only re-lock.
 */
export async function electAndRejoin(options?: { force?: boolean }): Promise<void> {
	const previous = rustMaster.deck;
	electIfAuto(options);
	const next = _syncMaster();
	if (next === null || next === previous) return;
	await _reanchor(next, _lockedFollowers(next));
}

function _view(deck: DeckId, positionSec?: number): SyncDeckView {
	const st = deckStates[deck];
	return {
		beats: st.anlz?.beatgrid.beats ?? [],
		playing: st.playing,
		positionSec: positionSec ?? playheadMs(deck) / 1000,
		tempo: st.pitch
	};
}

function loadedDeck(deck: DeckId, what: string): DeckState {
	const st = deckStates[deck];
	if (st.stable_id === null) throw new Error(`${what}: deck ${deck} is not loaded`);
	return st;
}

function _pitchBounds(deck: DeckId): { min: number; max: number } {
	const range = pitchRanges[deck] / 100;
	return { min: Math.max(0.01, 1 - range), max: 1 + range };
}

function _setPlaying(deck: DeckId, playing: boolean): void {
	const st = deckStates[deck];
	st.playing = playing;
	st.audible = playing;
	st.transport_pending = false;
}

/** The page clock, in seconds. */
const _nowSec = (): number => performance.now() / 1000;

/**
 * Lock `follower` to `master`. `play` starts it on the lock; `reanchor`
 * re-phases a follower that is already locked, seeking only when its phase
 * is off (`reanchorNeedsSeek`). A plan that cannot phase-lock throws.
 */
async function _join(
	master: DeckId,
	follower: DeckId,
	options: {
		play?: boolean;
		reanchor?: boolean;
		masterAtSec?: number | undefined;
		followerAtSec?: number;
		/** A user seek: join on the beat nearest `followerAtSec`, not the nearest phased landing. */
		anchorOnBeat?: boolean;
	} = {}
): Promise<void> {
	const st = deckStates[follower];
	// No phase-lock trim may land between this join's tempo and the lock it
	// records: the trim would be relative to the old base.
	delete phaseLocks[follower];
	const masterState = deckStates[master];
	if (loadFences[follower] === Infinity || loadFences[master] === Infinity) {
		throw new Error('Beat Sync: cannot join while either deck is loading');
	}
	const followerGeneration = st.load_generation;
	const masterGeneration = masterState.load_generation;
	const followerId = st.stable_id;
	const masterId = masterState.stable_id;
	const current = () => st.load_generation === followerGeneration &&
		masterState.load_generation === masterGeneration &&
		st.stable_id === followerId && masterState.stable_id === masterId &&
		loadFences[follower] !== Infinity && loadFences[master] !== Infinity;
	const fv = _view(follower);
	const join = planRustFollowerJoin(_view(master, options.masterAtSec), fv, {
		leadSec: SYNC_LEAD_SEC,
		mode: syncModeForBeatSyncMax(uiPrefs.beat_sync_max, st.sync_mode),
		pitchRangePct: pitchRanges[follower],
		...(options.followerAtSec === undefined ? {} : { followerAtSec: options.followerAtSec }),
		...(options.anchorOnBeat === undefined ? {} : { anchorOnBeat: options.anchorOnBeat })
	});
	const cmds: EngineCommand[] = [{ type: 'tempo', deck: follower, ratio: join.tempo }];
	if (!options.reanchor || reanchorNeedsSeek(join, fv, SYNC_LEAD_SEC)) {
		cmds.push({ type: 'seek', deck: follower, position_ms: join.positionMs, quantize: false });
	}
	if (options.play) cmds.push({ type: 'play', deck: follower, playing: true });
	// Sent together: the engine applies what arrives before its next block
	// in that block, so tempo, position and start land as one.
	if (!current()) throw new Error('Beat Sync: join superseded by a replacement load');
	await Promise.all(cmds.map(send));
	if (!current()) throw new Error('Beat Sync: join superseded by a replacement load');
	st.pitch = join.tempo;
	st.sync_error = null;
	if (options.play) _setPlaying(follower, true);
	phaseLocks[follower] = {
		master,
		masterTempo: deckStates[master].pitch,
		stableId: st.stable_id,
		base: join.tempo,
		normalization: join.plan.tempoNormalization,
		sent: join.tempo,
		busy: false,
		joinedAtSec: _nowSec(),
		overLineTicks: 0,
		userOffsetMs: 0
	};
}

function _lockHolds(deck: DeckId, lock: PhaseLock, master: DeckId): boolean {
	const st = deckStates[deck];
	return (
		master === lock.master &&
		master !== deck &&
		st.playing &&
		st.stable_id === lock.stableId &&
		effectiveBeatSync(st) &&
		// A master tempo move re-joins its followers; until it has, the base
		// belongs to the old tempo. The tolerance covers the engine's float echo.
		Math.abs(deckStates[master].pitch - lock.masterTempo) <= 1e-6 * lock.masterTempo
	);
}

/**
 * A dropped lock takes its trim with it ("a trim never outlives its error"):
 * the base tempo goes back when the deck is still on the lock's track, is not
 * now the master (#1134: a promoted deck's tempo is the master's, which its
 * own followers are locked to), and still plays the lock's own write - the
 * last trim, or a frame echo of the base. A tempo the DJ set since is theirs.
 */
function _dropLock(deck: DeckId, lock: PhaseLock): void {
	delete phaseLocks[deck];
	const st = deckStates[deck];
	if (lock.sent === lock.base || st.stable_id !== lock.stableId || rustMaster.deck === deck) return;
	const near = (a: number, b: number): boolean => Math.abs(a - b) <= 1e-6 * b;
	if (!near(st.pitch, lock.sent) && !near(st.pitch, lock.base)) return;
	st.pitch = lock.base;
	const generation = st.load_generation;
	const stableId = st.stable_id;
	void send({ type: 'tempo', deck, ratio: lock.base }).catch((e: unknown) => {
		if (st.load_generation === generation && st.stable_id === stableId) {
			st.sync_error = `phase lock release failed: ${e instanceof Error ? e.message : String(e)}`;
		}
	});
}

/**
 * The continuous phase lock (NAE-19), run on every engine state frame
 * (30 Hz). A join sets a follower's tempo once; this keeps measuring its phase
 * against the master and sends a small trim on top of the join's base tempo
 * (`phaseLockDecision`), only when the trim moved enough to matter
 * (`phaseLockShouldSend`). A lost lock re-runs the join, which seeks.
 */
export function phaseLockTick(): void {
	const master = _syncMaster();
	for (const deck of DECKS) {
		const lock = phaseLocks[deck];
		if (lock === undefined || lock.busy) continue;
		if (master === null || !_lockHolds(deck, lock, master)) {
			_dropLock(deck, lock);
			continue;
		}
		const st = deckStates[deck];
		let decision: ReturnType<typeof phaseLockDecision>;
		try {
			const input = {
				masterBeats: deckStates[master].anlz?.beatgrid.beats ?? [],
				masterPositionSec: playheadMs(master) / 1000,
				masterTempo: deckStates[master].pitch,
				followerBeats: st.anlz?.beatgrid.beats ?? [],
				followerPositionSec: playheadMs(deck) / 1000,
				followerBaseTempo: lock.base,
				normalization: lock.normalization,
				pitchRangePct: pitchRanges[deck],
				sinceJoinSec: _nowSec() - lock.joinedAtSec,
				overLineTicks: lock.overLineTicks,
				userOffsetMs: lock.userOffsetMs
			};
			// Feed-forward may rewrite the base this tick. `trimming` is whether a
			// trim is already on the wire (sent !== the base the last tick settled),
			// not whether this tick's candidate base differs: using the candidate
			// made an in-phase follower look mid-trim and send on the next frame.
			const forwarded = phaseLockFeedForwardBase(input); // follows a grid tempo change (F4)
			decision = phaseLockDecision({
				...input,
				followerBaseTempo: forwarded,
				trimming: lock.sent !== lock.base
			});
		} catch (e) {
			// Thrown inside the state mirror: drop this lock and say why rather
			// than stop mirroring every deck.
			_dropLock(deck, lock);
			st.sync_error = `phase lock: ${e instanceof Error ? e.message : String(e)}`;
			continue;
		}
		// On an uneven grid the base follows the local tempo; every trim and the
		// release are relative to the base now.
		lock.base = decision.base;
		lock.overLineTicks = decision.overLineTicks;
		if (decision.action === 'reseek') {
			// A follower in its own loop is the DJ's: it is not seeked out of it.
			if (st.loop?.engaged) continue;
			lock.busy = true;
			const generation = st.load_generation;
			const stableId = st.stable_id;
			const masterState = deckStates[master];
			const masterGeneration = masterState.load_generation;
			const masterId = masterState.stable_id;
			// Share the load's scheduler claim: stale writes must not reach its new head.
			void runAutomaticRejoin(async () => {
				if (_syncMaster() !== master || phaseLocks[deck] !== lock || st.load_generation !== generation ||
					st.stable_id !== stableId || masterState.load_generation !== masterGeneration ||
					masterState.stable_id !== masterId || loadFences[deck] === Infinity ||
					loadFences[master] === Infinity) {
					if (phaseLocks[deck] === lock) lock.busy = false;
					return;
				}
				await _join(master, deck, { reanchor: true });
			}).catch((e: unknown) => {
				if (st.load_generation === generation && st.stable_id === stableId &&
					masterState.load_generation === masterGeneration && masterState.stable_id === masterId) {
					st.sync_error = `phase lock lost: ${e instanceof Error ? e.message : String(e)}`;
				}
				if (phaseLocks[deck] === lock) lock.busy = false;
			});
			continue;
		}
		if (!phaseLockShouldSend(lock.sent, decision.tempo, lock.base)) continue;
		lock.busy = true;
		const ratio = decision.tempo;
		void (async () => {
			try {
				await send({ type: 'tempo', deck, ratio });
				if (phaseLocks[deck] === lock) {
					lock.sent = ratio;
					st.pitch = ratio; // the page's view of its own write (_dropLock reads it)
				}
			} catch (e) {
				if (phaseLocks[deck] === lock) {
					delete phaseLocks[deck];
					st.sync_error = `phase lock trim failed: ${e instanceof Error ? e.message : String(e)}`;
				}
			} finally {
				lock.busy = false;
			}
		})();
	}
}

/** Re-phase playing followers after their master moved. A follower that
 * cannot lock keeps playing and says why in `sync_error`, as on Web Audio. */
async function _reanchor(
	master: DeckId,
	followers: readonly DeckId[],
	masterAtSec?: number
): Promise<void> {
	await Promise.all(
		followers.map((f) => {
			const state = deckStates[f];
			const generation = state.load_generation;
			const stableId = state.stable_id;
			return _join(master, f, { reanchor: true, masterAtSec }).catch((e: unknown) => {
				if (state.load_generation === generation && state.stable_id === stableId) {
					state.sync_error = e instanceof Error ? e.message : String(e);
				}
			});
		})
	);
}

function _lockedFollowers(master: DeckId): DeckId[] {
	return DECKS.filter(
		(d) => d !== master && deckStates[d].playing && effectiveBeatSync(deckStates[d])
	);
}

async function _play(deck: DeckId): Promise<void> {
	const st = loadedDeck(deck, 'play');
	const generation = st.load_generation;
	const stableId = st.stable_id;
	const syncClock = _syncMaster();
	const syncActive = effectiveBeatSync(st);
	if (syncClock !== null && syncClock !== deck && syncActive) {
		try {
			await _join(syncClock, deck, { play: true });
		} catch (e) {
			const detail = e instanceof Error ? e.message : String(e);
			if (st.load_generation === generation && st.stable_id === stableId) {
				st.sync_error = detail;
			}
			throw new Error(
				`Beat Sync: deck ${deck} could not phase-lock to deck ${syncClock} (${detail})`,
				{ cause: e }
			);
		}
		return;
	}
	await send({ type: 'play', deck, playing: true });
	_setPlaying(deck, true);
	st.sync_error = null;
	if (rustMaster.mode === 'auto' && syncClock === null) {
		electIfAuto();
		const elected = _syncMaster();
		if (elected !== null && elected !== deck && syncActive) {
			await _join(elected, deck, { reanchor: true });
		}
	}
}

async function _pause(deck: DeckId): Promise<void> {
	const st = loadedDeck(deck, 'pause');
	const at = playheadMs(deck);
	cancelArmedJump(deck);
	await send({ type: 'play', deck, playing: false });
	_setPlaying(deck, false);
	st.position_ms = at;
	// The memory cue a pause leaves, snapped as the Web Audio engine snaps it.
	st.cue_ms = rustCuePoint(st, at);
	if (rustMaster.deck === deck) await electAndRejoin();
}

/** The CUE button. Playing: back to the cue point and pause. Paused with no
 * cue: set it here. Paused with a cue: go to it. */
async function _pressCue(deck: DeckId): Promise<void> {
	const st = loadedDeck(deck, 'pressCue');
	if (st.playing) {
		const target = st.cue_ms ?? 0;
		cancelArmedJump(deck);
		await Promise.all([
			send({ type: 'seek', deck, position_ms: target, quantize: false }),
			send({ type: 'play', deck, playing: false })
		]);
		_setPlaying(deck, false);
		st.position_ms = target;
		if (rustMaster.deck === deck) await electAndRejoin();
		return;
	}
	if (st.cue_ms === null) {
		st.cue_ms = rustCuePoint(st, st.position_ms);
		return;
	}
	await _seek(deck, st.cue_ms, { quantize: true });
}

/** A seek: snapped to the deck's quantize grid when asked, out of an engaged
 * loop when it lands outside it, and re-phased when the deck is a follower
 * (or, with Beat Sync Max, when it is the master). */
async function _seek(deck: DeckId, ms: number, options: { quantize: boolean }): Promise<void> {
	const st = loadedDeck(deck, 'seek');
	const durMs = st.duration_ms ?? Infinity;
	if (!Number.isFinite(ms) || ms < 0 || ms > durMs) {
		throw new RangeError(`seek: ms must be within 0..${Math.round(durMs)}, got ${ms}`);
	}
	const { targetMs, exitLoop } = rustSeekTarget(
		options.quantize ? st : { ...st, quantize_enabled: false },
		ms
	);
	if (targetMs > durMs) {
		throw new RangeError(`seek: quantized target ${targetMs} exceeds duration ${durMs}`);
	}
	cancelArmedJump(deck);
	if (exitLoop) {
		// Rekordbox: a seek outside an engaged loop leaves it.
		await send({ type: 'loop', deck, loop: null });
		displayLoops[deck] = null;
		st.loop = null;
	}
	const master = _syncMaster();
	if (st.playing && effectiveBeatSync(st) && master !== null && master !== deck) {
		await _join(master, deck, { followerAtSec: targetMs / 1000, anchorOnBeat: options.quantize });
		st.position_ms = targetMs;
		return;
	}
	await send({ type: 'seek', deck, position_ms: targetMs, quantize: false });
	st.position_ms = targetMs;
	if (st.playing && uiPrefs.beat_sync_max && master === deck) {
		await _reanchor(deck, _lockedFollowers(deck), targetMs / 1000);
	}
}

async function _setTempo(deck: DeckId, ratio: number): Promise<void> {
	const st = loadedDeck(deck, 'setTempoRatio');
	if (!Number.isFinite(ratio) || ratio <= 0) {
		throw new RangeError(`setTempoRatio: ratio must be > 0, got ${ratio}`);
	}
	const rangePct = pitchRanges[deck];
	if (Math.abs(ratio - 1) * 100 > rangePct + 1e-9) {
		throw new RangeError(
			`setTempoRatio: ratio ${ratio} outside the selected +-${rangePct}% range on deck ${deck}`
		);
	}
	const master = _syncMaster();
	if (st.playing && effectiveBeatSync(st) && master !== null && master !== deck) {
		throw new Error(`setTempoRatio: disable Beat Sync before changing follower deck ${deck}`);
	}
	await send({ type: 'tempo', deck, ratio });
	st.pitch = ratio;
	if (master === deck) await _reanchor(deck, _lockedFollowers(deck));
}

async function _setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {
	if (typeof enabled !== 'boolean') throw new TypeError('setBeatSync: enabled must be boolean');
	const st = deckStates[deck];
	st.beat_sync_enabled = enabled;
	if (!enabled) {
		st.sync_error = null;
		return;
	}
	// Same contract as Web Audio: the flag keeps the DJ's choice, a gridless
	// deck has nothing to lock to, and that is said out loud.
	if (gridFeaturesInert(st)) {
		notify(`Deck ${deck} BEAT SYNC ${gridFeatureInertTip(st)}`, 'info');
		return;
	}
	if (!st.playing) return;
	let master = _syncMaster();
	if (master === null) {
		if (rustMaster.mode === 'locked' && rustMaster.deck !== null) return;
		electIfAuto();
		master = _syncMaster();
	}
	if (master === null || master === deck) return;
	try {
		await _join(master, deck);
	} catch (e) {
		st.beat_sync_enabled = false;
		const { min, max } = _pitchBounds(deck);
		const detail = e instanceof Error ? e.message : String(e);
		throw new Error(`cannot phase-lock within pitch [${min}, ${max}] (BAR): ${detail}`, {
			cause: e
		});
	}
}

async function _setDeckMaster(deck: DeckId, lock: boolean | undefined): Promise<void> {
	const st = loadedDeck(deck, 'setDeckMaster');
	const applyLock = (): void => {
		if (lock === true) rustMaster.mode = 'locked';
		else if (lock === false) {
			rustMaster.mode = 'auto';
			if (!st.playing) electIfAuto();
		}
	};
	if (!st.playing) {
		if (lock === false && rustMaster.deck === deck) return applyLock();
		assertPausedMasterSelectionAllowed(
			deck,
			st.audible,
			pausedMasterSelectionBlockers(deck, deckStates)
		);
		_assignMaster(deck);
		return applyLock();
	}
	const followers = masterSwitchFollowers(deck, deckStates).filter((d) =>
		effectiveBeatSync(deckStates[d])
	);
	_assignMaster(deck);
	await _reanchor(deck, followers);
	applyLock();
}

export async function decideOnPage(command: PerformanceCommand): Promise<void> {
	switch (command.type) {
		case 'play':
			// Quantized and scheduled launches go to the engine, which refuses
			// them by name rather than starting off the grid.
			if (command.quantize === true || command.start_at_context_sec !== undefined) {
				await send(command);
				return;
			}
			return command.playing ? _play(command.deck) : _pause(command.deck);
		case 'cue':
			return _pressCue(command.deck);
		case 'seek':
			return _seek(command.deck, command.position_ms, { quantize: true });
		case 'tempo':
			return _setTempo(command.deck, command.ratio);
		case 'beat_sync':
			return _setBeatSync(command.deck, command.enabled);
		case 'sync_mode': {
			if (command.mode !== 'beat' && command.mode !== 'bar') {
				throw new TypeError(`setSyncMode: invalid sync mode ${String(command.mode)}`);
			}
			const st = deckStates[command.deck];
			st.sync_mode = command.mode;
			const master = _syncMaster();
			if (st.playing && effectiveBeatSync(st) && master !== null && master !== command.deck) {
				await _join(master, command.deck, { reanchor: true });
			}
			return;
		}
		case 'master':
			return _setDeckMaster(command.deck, command.lock);
		case 'quantize': {
			if (typeof command.enabled !== 'boolean') {
				throw new TypeError('setQuantize: enabled must be boolean');
			}
			await send(command);
			const st = deckStates[command.deck];
			st.quantize_enabled = command.enabled;
			if (command.enabled && gridFeaturesInert(st)) {
				notify(`Deck ${command.deck} QUANTIZE ${gridFeatureInertTip(st)}`, 'info');
			}
			return;
		}
		case 'quantize_grid':
			if (command.beats !== 1 && command.beats !== 4 && command.beats !== 8) {
				throw new TypeError('setQuantizeGrid: beats must be 1, 4, or 8');
			}
			await send(command);
			deckStates[command.deck].quantize_grid_beats = command.beats;
			return;
		default:
			throw new Error(`${command.type} is not decided by the page in Rust engine mode`);
	}
}

// ------------------------------------------------------------------ hot cues

/** Armed jumps waiting on their downbeat, per deck. */
const armedJumps: Partial<Record<DeckId, ReturnType<typeof setTimeout>>> = {};

export function cancelArmedJump(deck: DeckId): void {
	const t = armedJumps[deck];
	if (t !== undefined) clearTimeout(t);
	delete armedJumps[deck];
}

/**
 * The page's hot-cue logic (`hot_cue_*` in performance-ipc) driving this
 * engine: the same slots, trust gate and `planHotCueTrigger` decision. An
 * immediate trigger is a quantized seek. An armed one waits for the planned
 * downbeat on the page clock, then seeks as far past the cue as the music
 * already went, so it lands on the beat even when the timer fires late.
 */
export const rustHotCueDriver: PerformanceHotCueDriver = {
	stableId: (deck) => deckStates[deck].stable_id,
	refresh: async (deck) => {
		const st = loadedDeck(deck, 'hot cue refresh');
		const id = st.stable_id as string;
		const slots = await fetchHotCueSlots(id);
		if (st.stable_id !== id) return;
		st.hot_cues = hotCuesFromAnlz(slots.flatMap((s) => (s.cue === null ? [] : [s.cue])));
		st.hot_cue_revisions = _hotCueRevisionsFrom(slots);
	},
	triggerState: (deck, slot) => {
		const st = deckStates[deck];
		return {
			cue: st.hot_cues.find((c) => c.slot === slot) ?? null,
			playing: st.playing,
			loopEngaged: st.loop !== null && st.loop.engaged,
			positionSec: playheadMs(deck) / 1000,
			beats: st.anlz?.beatgrid.beats ?? []
		};
	},
	jump: (deck, positionMs) => _seek(deck, positionMs, { quantize: true }),
	arm: async (deck, positionMs, armAt) => {
		const st = loadedDeck(deck, 'armHotCueTrigger');
		const nowPositionSec = playheadMs(deck) / 1000;
		// A resolver arm point (waveform seek) is resolved on this live playhead.
		const armAtPositionSec = resolveArmAtPosition(armAt, nowPositionSec);
		cancelArmedJump(deck);
		const delaySec = (armAtPositionSec - nowPositionSec) / st.pitch;
		const landsAt = _nowSec() + delaySec;
		const generation = st.load_generation;
		armedJumps[deck] = setTimeout(() => {
			delete armedJumps[deck];
			if (st.load_generation !== generation || !st.playing) return;
			const target = lateJumpPositionMs(positionMs, _nowSec() - landsAt, st.pitch);
			_seek(deck, target, { quantize: false }).catch((e: unknown) => {
				notify(`Deck ${deck} hot cue: ${e instanceof Error ? e.message : String(e)}`, 'error');
			});
		}, delaySec * 1000);
		return landsAt;
	},
	contextTimeNowSec: _nowSec
};
