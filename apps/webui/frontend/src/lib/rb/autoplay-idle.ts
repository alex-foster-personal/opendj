/**
 * PLAY-09 / issue #1878: disarm AutoPlay once every deck has been stopped long
 * enough with no pending master promotion (hunt `stall:autoplay-idle`).
 */
import {
	AUTO_PLAY_IDLE_DISARM_MS,
	anyDeckPlaying,
	shouldDisarmAutoPlayIdle,
	type AutoPlayDeckSnap
} from '$lib/rb/auto-play';

export { AUTO_PLAY_IDLE_DISARM_MS };

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

export function planAutoPlayIdleDisarm(input: {
	enabled: boolean;
	snaps: readonly AutoPlayDeckSnap[];
	pending_master: boolean;
	now_ms: number;
	stall_active: boolean;
}): AutoPlayIdleDisarmPlan {
	if (!input.enabled) {
		_idleSinceMs = null;
		return { action: 'continue', idle_since_ms: null };
	}
	const anyPlaying = anyDeckPlaying(input.snaps);
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
