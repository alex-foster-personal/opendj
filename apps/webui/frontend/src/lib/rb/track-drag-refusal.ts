/**
 * Why a library row cannot be dragged onto a deck, or null when it can.
 *
 * Pins 8ba0b15d975b and 72be3e505510 (the maintainer, Wed 2 Sep 2026) are the same
 * defect seen from two sides. Dragging a row that cannot load called
 * `preventDefault()` and returned - the drag simply never started, with no
 * cursor change, no toast, nothing - so it read as "can't click and drag from
 * library, deck". Double-clicking the SAME row explained itself properly
 * ("streaming track - deck load not implemented"), which is how the second pin
 * arrived carrying its own diagnosis.
 *
 * One condition, two levels of honesty. The reasons live here so both paths
 * give the same answer in the same words, rather than one path being mute.
 */

import type { FileAvailabilityStatus } from './api-rb';

export interface DraggableRow {
	file_exists: boolean | null;
	file_availability?: FileAvailabilityStatus | null;
	/** null = not known to be streaming. The row model uses `boolean | null`,
	 * and the rest of the code tests `=== true`, so null is treated as "not
	 * streaming" here too rather than as a third state. */
	is_streaming?: boolean | null;
}

/**
 * Null when the row can be dragged to a deck; otherwise the reason, phrased
 * for a toast and worded EXACTLY as the double-click path in BrowserPanel
 * words the same refusal - pinned by the test rather than shared as a
 * constant. Sharing was tried and reverted: importing it into BrowserPanel
 * pushed that file's fan-out past the coupling ratchet, for a file already at
 * its ceiling, and gzip already collapses the repeated sentence so it bought
 * nothing. A duplicated string with a test on it is the cheaper trade here.
 */
export function trackDragRefusal(row: DraggableRow): string | null {
	// The server's own availability status names a streaming URI too, so a row
	// whose is_streaming flag has not hydrated yet is still refused as streaming
	// up front, never as a "missing on disk" broken link (issue #3934).
	if (row.is_streaming === true || row.file_availability === 'streaming') {
		return 'streaming track - deck load not implemented (see PARITY-TODO)';
	}
	if (row.file_availability === 'AVAILABILITY_PENDING' || row.file_exists === null) {
		return 'cannot load: availability still checking (wait for disk probe)';
	}
	if (row.file_exists === false) {
		return 'cannot load: audio file missing on disk (broken link)';
	}
	return null;
}
