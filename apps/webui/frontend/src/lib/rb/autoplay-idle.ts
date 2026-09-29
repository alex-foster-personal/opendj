/**
 * PLAY-09 / issue #1878: disarm AutoPlay once every deck has been stopped long
 * enough with no pending master promotion (hunt `stall:autoplay-idle`).
 * PLAY-12 / issue #3884: 30s silent stall, 31s post-playback disarm, 3min
 * armed-empty grace when enabled with nothing playing.
 */

/**
 * Both decks stopped while AutoPlay stays armed (issue #1878 / hunt
 * `stall:autoplay-idle`). Must stay greater than AUTO_PLAY_SILENT_STALL_MS so
 * the stall fires before disarm (PLAY-08 retain_stall).
 */
export const AUTO_PLAY_IDLE_DISARM_MS = 31_000;

/** PLAY-08: raise a stall when AutoPlay is armed but nothing is playing (issue #2153). */
export const AUTO_PLAY_SILENT_STALL_MS = 30_000;

/** PLAY-12: disarm when armed with no deck playing and none has started since enable. */
export const AUTO_PLAY_ARMED_EMPTY_DISARM_MS = 180_000;

export type AutoPlayIdleDisarmPlan =
	| { action: 'continue'; idle_since_ms: number | null }
	| { action: 'disarm'; retain_stall: boolean };

/** Module clock for the poll; reset on install/uninstall and when playback resumes. */
let _idleSinceMs: number | null = null;

/** True while AutoPlay was enabled with nothing playing and no deck has started yet. */
let _armedEmptyActive = false;

let _lastEnabled = false;

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

export function effectiveAutoPlayIdleDisarmMs(input: { armed_empty_active: boolean }): number {
	return input.armed_empty_active ? AUTO_PLAY_ARMED_EMPTY_DISARM_MS : AUTO_PLAY_IDLE_DISARM_MS;
}

/**
 * Enter armed-empty mode on enable when nothing is playing; clear when disabled.
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
 * Disarm AutoPlay once every deck has been stopped long enough and no master
 * promotion is still settling.
 */
export function shouldDisarmAutoPlayIdle(input: {
	enabled: boolean;
	any_playing: boolean;
	pending_master: boolean;
	silence_recovering?: boolean | undefined;
	idle_since_ms: number | null;
	now_ms: number;
	armed_empty_active: boolean;
}): boolean {
	if (!input.enabled) return false;
	if (input.any_playing) return false;
	if (input.pending_master) return false;
	if (input.silence_recovering === true) return false;
	if (input.idle_since_ms === null) return false;
	const threshold = effectiveAutoPlayIdleDisarmMs({
		armed_empty_active: input.armed_empty_active
	});
	return input.now_ms - input.idle_since_ms >= threshold;
}

export function planAutoPlayIdleDisarm(input: {
	enabled: boolean;
	snaps: readonly { playing: boolean }[];
	pending_master: boolean;
	silence_recovering?: boolean | undefined;
	now_ms: number;
	stall_active: boolean;
}): AutoPlayIdleDisarmPlan {
	if (!input.enabled) {
		_idleSinceMs = null;
		return { action: 'continue', idle_since_ms: null };
	}
	const anyPlaying = input.snaps.some((d) => d.playing);
	if (anyPlaying || input.pending_master || input.silence_recovering === true) {
		_idleSinceMs = null;
		return { action: 'continue', idle_since_ms: null };
	}
	if (_idleSinceMs === null) {
		_idleSinceMs = input.now_ms;
	}
	const armedEmptyActive = isAutoPlayArmedEmptyActive();
	if (
		!shouldDisarmAutoPlayIdle({
			enabled: input.enabled,
			any_playing: anyPlaying,
			pending_master: input.pending_master,
			silence_recovering: input.silence_recovering,
			idle_since_ms: _idleSinceMs,
			now_ms: input.now_ms,
			armed_empty_active: armedEmptyActive
		})
	) {
		return { action: 'continue', idle_since_ms: _idleSinceMs };
	}
	_idleSinceMs = null;
	_armedEmptyActive = false;
	return { action: 'disarm', retain_stall: input.stall_active };
}

export function shouldRaiseAutoPlaySilentStall(input: {
	enabled: boolean;
	any_playing: boolean;
	pending_master: boolean;
	silence_recovering?: boolean | undefined;
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
	if (input.stall_active) return false;
	if (input.idle_since_ms === null) return false;
	if (input.source_stable_id === null || input.source_stable_id === '') return false;
	return input.now_ms - input.idle_since_ms >= AUTO_PLAY_SILENT_STALL_MS;
}

export function applyAutoPlayIdleDisarmAction(
	plan: AutoPlayIdleDisarmPlan,
	onDisarm: (retainStall: boolean) => void
): 'continue' | 'disarmed' {
	if (plan.action !== 'disarm') return 'continue';
	onDisarm(plan.retain_stall);
	return 'disarmed';
}

export function clearAutoPlayChartedOrder(
	clearKey: () => void,
	publishEmpty: () => void
): void {
	clearKey();
	publishEmpty();
}
