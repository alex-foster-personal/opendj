/**
 * performance-ipc for the held grid-jump chord test (#12276): the real module,
 * except that the UI command boundary records each command instead of sending
 * it, so a held chord can be counted as exactly one `grid_jump` dispatch.
 * Imported by relative path so the test's alias does not loop back here.
 */
export * from '../../../src/lib/rb/performance-ipc.svelte';

export type RecordedCommand = { type: string; [key: string]: unknown };

export const recorded: RecordedCommand[] = [];

/** Record the command the listener sent and answer ok, as the real bus does. */
export async function runPerformanceCommandFromUi(
	command: RecordedCommand
): Promise<{ ok: true }> {
	recorded.push({ ...command });
	return { ok: true };
}
