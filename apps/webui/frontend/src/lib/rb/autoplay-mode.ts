/**
 * Shared AutoPlay mode description for TopBar tooltip + library explainer.
 * Pure strings - no DOM. Keep both surfaces from drifting.
 */

export type AutoPlayModeKind = 'greedy' | 'reach' | 'enforce' | 'off';

export function describeAutoPlayMode(prefs: {
	auto_play_enabled: boolean;
	auto_play_enforce_order: boolean;
	auto_play_maximize_reach: boolean;
}): { mode: AutoPlayModeKind; short: string; detail: string } {
	if (!prefs.auto_play_enabled) {
		return {
			mode: 'off',
			short: 'AutoPlay OFF',
			detail: 'No automatic next-track handoff.'
		};
	}
	if (prefs.auto_play_enforce_order) {
		return {
			mode: 'enforce',
			short: 'Enforce play order',
			detail:
				'Walks strict playlist membership after the current track (skips missing/stub audio). Ignores key/BPM gates and maximize-reach.'
		};
	}
	if (prefs.auto_play_maximize_reach) {
		return {
			mode: 'reach',
			short: 'Maximize reach (Warnsdorff)',
			detail:
				'Among key +-1 and Beat Sync BPM-compatible next tracks, prefers fewest onward options so later tracks stay reachable (dead ends last).'
		};
	}
	return {
		mode: 'greedy',
		short: 'Greedy earliest',
		detail:
			'Picks the earliest unplayed playlist track with Camelot key +-1 and BPM inside the Beat Sync pitch window.'
	};
}
