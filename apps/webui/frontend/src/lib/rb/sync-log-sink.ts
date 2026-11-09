/**
 * Where SYNCLOG-9650 sync lines go (formatted by sync-drift.ts): the console and
 * the perf ring for DevTools, and the engine's client log so they land on disk
 * in `engine-warn.log` and `webui-client-errors-<date>.log` under the app's
 * logs directory, each starting with its tag (`SYNC_DRIFT`, `SYNC_RESNAP`).
 * Shared by both engines.
 */
import { reportClientError } from '$lib/client-error-reporting';
import { recordPerfEvent } from '$lib/rb/perf-event-log';
import { formatSyncLogLine, type SyncLogTag, type SyncLogValue } from '$lib/rb/sync-drift';

export function emitSyncLog(
	tag: SyncLogTag,
	deck: 1 | 2 | 3 | 4,
	fields: Record<string, SyncLogValue>
): string {
	const line = formatSyncLogLine(tag, fields);
	recordPerfEvent(tag, line, deck, 'info');
	reportClientError(line, { source: 'sync-telemetry', sync_tag: tag, deck }, 'ui-error');
	return line;
}
