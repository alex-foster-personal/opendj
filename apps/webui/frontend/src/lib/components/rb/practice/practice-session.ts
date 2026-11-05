/**
 * Practice route command session (ADR-0137). Replica controls dispatch through the same
 * action glue as a controller, and the glue's mixer, transport and deck commands go through
 * dispatchPerformanceCommand, which refuses with no active command session. /performance and
 * Trackify start theirs with installPerformanceBrowserIpc; /practice does the same, claiming the
 * performance mount first so a retiring page's teardown sees a newer owner.
 *
 * Requirements (mini-PRD):
 *   ✔︎ installPracticeSession starts a command session and returns its uninstall.
 *     [if] a fader move on /practice is refused for want of a command session [then] ⛔️
 *     [if] uninstall leaves the session active after the route unmounts [then] ⛔️
 */

import {
	claimPerformanceMount,
	installPerformanceBrowserIpc
} from '$lib/rb/performance-ipc.svelte';

export function installPracticeSession(): () => void {
	claimPerformanceMount();
	return installPerformanceBrowserIpc();
}
