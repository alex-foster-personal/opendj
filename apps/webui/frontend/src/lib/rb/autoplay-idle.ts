/**
 * PLAY-09 / issue #1878: disarm AutoPlay once every deck has been stopped long
 * enough with no pending master promotion (hunt `stall:autoplay-idle`).
 */

/**
 * Both decks stopped while AutoPlay stays armed (issue #1878 / hunt
 * `stall:autoplay-idle`). Matches the hunt collector threshold.
 */
export const AUTO_PLAY_IDLE_DISARM_MS = 10_000;

export type AutoPlayIdleDisarmPlan =
	| { action: 'continue'; idle_since_ms: number | null }
	| { action: 'disarm'; retain_stall: boolean };

/** Module clock for the poll; reset on install/uninstall and when playback resumes. */
let _idleSinceMs: number | null = null;

export function resetAutoPlayIdleClock(): void {
	_idleSinceMs = null;
}

export function readAutoPlayIdleSinceMs(): number | null {
	return _idleSinceMs;
}

/**
 * Disarm AutoPlay once every deck has been stopped long enough and no master
 * promotion is still settling.
 */
export function shouldDisarmAutoPlayIdle(input: {
	enabled: boolean;
	any_playing: boolean;
	pending_master: boolean;
	idle_since_ms: number | null;
	now_ms: number;
}): boolean {
	if (!input.enabled) return false;
	if (input.any_playing) return false;
	if (input.pending_master) return false;
	if (input.idle_since_ms === null) return false;
	return input.now_ms - input.idle_since_ms >= AUTO_PLAY_IDLE_DISARM_MS;
}

export function planAutoPlayIdleDisarm(input: {
	enabled: boolean;
	snaps: readonly { playing: boolean }[];
	pending_master: boolean;
	now_ms: number;
	stall_active: boolean;
}): AutoPlayIdleDisarmPlan {
	if (!input.enabled) {
		_idleSinceMs = null;
		return { action: 'continue', idle_since_ms: null };
	}
	const anyPlaying = input.snaps.some((d) => d.playing);
	if (anyPlaying || input.pending_master) {
		_idleSinceMs = null;
		return { action: 'continue', idle_since_ms: null };
	}
	if (_idleSinceMs === null) {
		_idleSinceMs = input.now_ms;
	}
	if (
		!shouldDisarmAutoPlayIdle({
			enabled: input.enabled,
			any_playing: anyPlaying,
			pending_master: input.pending_master,
			idle_since_ms: _idleSinceMs,
			now_ms: input.now_ms
		})
	) {
		return { action: 'continue', idle_since_ms: _idleSinceMs };
	}
	_idleSinceMs = null;
	return { action: 'disarm', retain_stall: input.stall_active };
}
