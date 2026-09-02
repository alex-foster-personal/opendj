/** Pure helper for whether a playlist detail's diff is worth rendering (GUARD-11). */

import type { PlaylistDetail } from './api';

/** The route serves an empty PlaylistDiff() when no rekordbox/djay sync diff
 * has been computed for this playlist -- that must render as an absent
 * section, not four empty-but-present columns (issue #775). */
export function hasComputedDiff(diff: PlaylistDetail['diff']): boolean {
	return (
		diff.rb_only.length > 0 ||
		diff.both.length > 0 ||
		diff.djay_only.length > 0 ||
		diff.conflicts.length > 0
	);
}
