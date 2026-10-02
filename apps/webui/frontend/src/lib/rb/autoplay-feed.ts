/**
 * Candidate-feed snapshot policy for AutoPlay. Pure and instance-scoped.
 *
 * The activation sort order remains stable while AutoPlay runs. Filters define
 * candidate membership, so changing one replaces the feed and advances its
 * epoch at the caller. This keeps filtered-out rows out of both the picker and
 * the charted-order memo without re-sorting a running set.
 */
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';

export interface AutoPlayFeedDecision {
	publish: readonly AutoPlayTrackRow[] | null;
	snapshotted: boolean;
}

export interface AutoPlayFeedSnapshot {
	step(
		enabled: boolean,
		playlistScope: string | null,
		viewRows: readonly AutoPlayTrackRow[],
		filterKey?: string,
		membershipHydrating?: boolean
	): AutoPlayFeedDecision;
	matches(viewRows: readonly Pick<AutoPlayTrackRow, 'stable_id'>[]): boolean;
	readonly active: boolean;
}

export function createAutoPlayFeedSnapshot(): AutoPlayFeedSnapshot {
	let active = false;
	let snapshotIds: readonly string[] | null = null;
	let activePlaylistScope: string | null = null;
	let activeFilterKey = '';
	return {
		get active(): boolean {
			return active;
		},
		matches(viewRows: readonly Pick<AutoPlayTrackRow, 'stable_id'>[]): boolean {
			return (
				active &&
				snapshotIds !== null &&
				snapshotIds.length === viewRows.length &&
				snapshotIds.every((id, index) => id === viewRows[index].stable_id)
			);
		},
		step(
			enabled: boolean,
			playlistScope: string | null,
			viewRows: readonly AutoPlayTrackRow[],
			filterKey = '',
			membershipHydrating = false
		): AutoPlayFeedDecision {
			if (!enabled) {
				active = false;
				snapshotIds = null;
				activePlaylistScope = null;
				activeFilterKey = '';
				return { publish: viewRows.slice(), snapshotted: false };
			}
			if (active && playlistScope !== activePlaylistScope) {
				// A new playlist is a new candidate set. Do not arm a transient
				// empty pane while its rows are still hydrating.
				active = false;
				activePlaylistScope = playlistScope;
				if (viewRows.length === 0) return { publish: [], snapshotted: false };
			}
			if (active) {
				if (filterKey !== activeFilterKey) {
					// Preserve the activation sort for a plain re-sort, but honor
					// the operator's current filter as the candidate membership.
					activeFilterKey = filterKey;
					snapshotIds = viewRows.map((row) => row.stable_id);
					return { publish: viewRows.slice(), snapshotted: false };
				}
				return { publish: null, snapshotted: false };
			}
			if (membershipHydrating) {
				active = false;
				snapshotIds = null;
				return { publish: viewRows.slice(), snapshotted: false };
			}
			if (viewRows.length === 0) {
				return { publish: viewRows.slice(), snapshotted: false };
			}
			activePlaylistScope = playlistScope;
			active = true;
			activeFilterKey = filterKey;
			snapshotIds = viewRows.map((row) => row.stable_id);
			return { publish: viewRows.slice(), snapshotted: true };
		}
	};
}
