/**
 * PLAY-09 / issue #1878, PLAY-12 / issue #3884, revised by PLAY-18 (Tue 6 Oct 2026):
 * the idle clock behind the 30 s silent stall.
 *
 * PLAY-18: idle NEVER writes the operator's AutoPlay toggle. It used to call
 * `setAutoPlayEnabled(false)` after 31 s (3 min when armed with nothing
 * playing), which made the mirror read `disabled`, exactly what the maintainer turning it
 * off reads (05:21Z and 05:58Z Tue 6 Oct 2026). Now AutoPlay stays enabled,
 * the mirror reports `armed: false` with reason `no-deck-playing`, and it
 * re-arms by itself the moment a deck plays. Only a user turns it off.
 */

/**
 * The post-playback idle threshold the error hunt waits out
 * (`tests/e2e/autoplay-error-hunt.spec.ts`). Greater than
 * AUTO_PLAY_SILENT_STALL_MS so the stall is up by then.
 */
export const AUTO_PLAY_IDLE_MS = 31_000;

/** PLAY-08: raise a stall when AutoPlay is armed but nothing is playing (issue #2153). */
export const AUTO_PLAY_SILENT_STALL_MS = 30_000;

/** Module clock for the poll; reset on install/uninstall and when playback resumes. */
let _idleSinceMs: number | null = null;

/** True while AutoPlay was enabled with nothing playing and no deck has started yet. */
let _armedEmptyActive = false;

let _lastEnabled = false;

function _isEngineRecoveryStop(reason: string | null | undefined): boolean {
	return typeof reason === 'string' && reason !== '';
}

export function resetAutoPlayIdleClock(): void {
	_idleSinceMs = null;
	resetAutoPlayArmedEmptyClock();
}

export function resetAutoPlayArmedEmptyClock(): void {
	_armedEmptyActive = false;
	_lastEnabled = false;
}

export function readAutoPlayIdleSinceMs(): number | null {
	return _idleSinceMs;
}

export function isAutoPlayArmedEmptyActive(): boolean {
	return _armedEmptyActive;
}

/**
 * Enter armed-empty mode on enable when nothing is playing; clear when disabled.
 * Armed-empty suppresses the silent stall until a deck first plays (PLAY-12):
 * switching AutoPlay on over silent decks is not a stall.
 */
export function noteAutoPlayArmedEmptyOnEnable(input: {
	enabled: boolean;
	any_playing: boolean;
	now_ms: number;
}): void {
	const justEnabled = input.enabled && !_lastEnabled;
	_lastEnabled = input.enabled;
	if (!input.enabled) {
		_armedEmptyActive = false;
		return;
	}
	if (justEnabled && !input.any_playing) {
		_armedEmptyActive = true;
		_idleSinceMs = input.now_ms;
	}
}

/** Exit armed-empty mode once any deck starts playing. */
export function noteAutoPlayPlaybackStarted(): void {
	_armedEmptyActive = false;
}

/**
 * PLAY-18: advance the idle clock for one poll and return when idle began (null
 * while anything is playing, a master promotion is settling, silence recovery
 * runs, or the engine stopped the decks itself, bug #58). It never disarms.
 */
export function trackAutoPlayIdleClock(input: {
	enabled: boolean;
	snaps: readonly { playing: boolean }[];
	pending_master: boolean;
	silence_recovering?: boolean | undefined;
	/** Bug #58: the engine stopped the decks while recovering its graph. */
	engine_recovery_stop?: string | null | undefined;
	now_ms: number;
}): number | null {
	const idle =
		input.enabled &&
		!input.snaps.some((d) => d.playing) &&
		!input.pending_master &&
		input.silence_recovering !== true &&
		!_isEngineRecoveryStop(input.engine_recovery_stop);
	if (!idle) {
		_idleSinceMs = null;
		return null;
	}
	_idleSinceMs ??= input.now_ms;
	return _idleSinceMs;
}

export function shouldRaiseAutoPlaySilentStall(input: {
	enabled: boolean;
	any_playing: boolean;
	pending_master: boolean;
	silence_recovering?: boolean | undefined;
	/** Bug #58: the engine stopped the decks while recovering its graph. */
	engine_recovery_stop?: string | null | undefined;
	stall_active: boolean;
	idle_since_ms: number | null;
	now_ms: number;
	source_stable_id: string | null;
	armed_empty_active: boolean;
}): boolean {
	if (!input.enabled) return false;
	if (input.armed_empty_active) return false;
	if (input.any_playing) return false;
	if (input.pending_master) return false;
	if (input.silence_recovering === true) return false;
	if (_isEngineRecoveryStop(input.engine_recovery_stop)) return false;
	if (input.stall_active) return false;
	if (input.idle_since_ms === null) return false;
	if (input.source_stable_id === null || input.source_stable_id === '') return false;
	return input.now_ms - input.idle_since_ms >= AUTO_PLAY_SILENT_STALL_MS;
}

export function clearAutoPlayChartedOrder(
	clearKey: () => void,
	publishEmpty: () => void
): void {
	clearKey();
	publishEmpty();
}

/**
 * PLAY-15: how long a deck may play with no master before AutoPlay says it is
 * blind. Covers the poll or two between an automatic demote and re-election;
 * an AutoPlay promotion in flight is excluded separately via `pending_master`.
 */
export const AUTO_PLAY_NO_MASTER_STALL_MS = 5_000;

/**
 * PLAY-15: raise `no-playing-master` once a deck has been playing with no
 * playing master for AUTO_PLAY_NO_MASTER_STALL_MS. AutoPlay only arms off the
 * playing master, so this state silently ends the set at the last track's end.
 */
export function shouldRaiseAutoPlayNoMasterStall(input: {
	enabled: boolean;
	any_playing: boolean;
	playing_master: boolean;
	pending_master: boolean;
	stall_active: boolean;
	no_master_since_ms: number | null;
	now_ms: number;
}): boolean {
	if (!input.enabled) return false;
	if (!input.any_playing) return false;
	if (input.playing_master) return false;
	if (input.pending_master) return false;
	if (input.stall_active) return false;
	if (input.no_master_since_ms === null) return false;
	return input.now_ms - input.no_master_since_ms >= AUTO_PLAY_NO_MASTER_STALL_MS;
}
