/**
 * Safe ratings sync actions for command search and the Settings overlay
 * (#12988): a status read and a dry run, both through the typed API client.
 * Neither ever sends a live write: the dry run posts live=false, and a live
 * apply stays behind the CLI and the rekordbox writeback gate
 * ((private path), "Conflict policy": dry-run first).
 *
 * Each action resolves to the sentence the user reads. A missing
 * ratings-diff.csv is reported as such, never as an empty dry run; a failed
 * request rejects with the client's own error.
 */
import {
	dryRunRbDjayRatingsSync,
	getRbDjayRatingsSyncStatus,
	type RbDjayRatingsDryRun,
	type RbDjayRatingsSyncStatus
} from '$lib/api';
import type { SettingActionId } from './catalog-types';

const BUILD_HINT = 'build one with the metadata plan (POST /api/v1/rb-djay-sync/metadata/plan)';

function _updatedAt(mtime: number): string {
	return `${new Date(mtime * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

function _gate(status: RbDjayRatingsSyncStatus): string {
	return `Rekordbox writeback gate is ${status.writeback_enabled ? 'on' : 'off'}.`;
}

function _diffAt(status: RbDjayRatingsSyncStatus): string {
	return `ratings diff artifact at ${status.ratings_diff.path}`;
}

/** The sentence for a ratings sync status read: where the diff is and how fresh, or that there is none. */
export function ratingsSyncStatusMessage(status: RbDjayRatingsSyncStatus): string {
	const diff = status.ratings_diff;
	if (diff.mtime === null) return `No ${_diffAt(status)} yet: ${BUILD_HINT}. ${_gate(status)}`;
	return `Ratings diff at ${diff.path}, updated ${_updatedAt(diff.mtime)}. ${_gate(status)}`;
}

/** The sentence for a ratings dry run: rows per resolution, or why nothing was dry run. */
export function ratingsSyncDryRunMessage(
	status: RbDjayRatingsSyncStatus,
	dryRun: RbDjayRatingsDryRun | null
): string {
	if (status.ratings_diff.mtime === null || dryRun === null) {
		return `Ratings sync dry run did not run: there is no ${_diffAt(status)}; ${BUILD_HINT}. Nothing was written.`;
	}
	if (dryRun.row_count === 0) {
		return 'Ratings sync dry run: the ratings diff has no rows, so there is nothing to sync. Nothing was written.';
	}
	const counts = Object.entries(dryRun.summary)
		.sort(([a], [b]) => a.localeCompare(b))
		.map(([resolution, count]) => `${resolution === '' ? '(unresolved)' : resolution} ${count}`)
		.join(', ');
	const rows = `${dryRun.row_count} ${dryRun.row_count === 1 ? 'row' : 'rows'}`;
	return `Ratings sync dry run: ${rows} (${counts}). Nothing was written.`;
}

/** Runs one catalog action and resolves with the sentence to show; a request failure rejects. */
export async function runSettingAction(action: SettingActionId): Promise<string> {
	const status = await getRbDjayRatingsSyncStatus();
	if (action === 'ratings_sync_status') return ratingsSyncStatusMessage(status);
	if (action === 'ratings_sync_dry_run') {
		// The service reads a missing diff as zero rows; ask first so that reads as "no artifact", not "nothing to sync".
		const dryRun = status.ratings_diff.mtime === null ? null : await dryRunRbDjayRatingsSync();
		return ratingsSyncDryRunMessage(status, dryRun);
	}
	const unhandled: never = action;
	throw new Error(`Unhandled setting action: ${String(unhandled)}`);
}
