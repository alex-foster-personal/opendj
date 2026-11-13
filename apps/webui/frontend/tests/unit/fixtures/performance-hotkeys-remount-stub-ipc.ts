/**
 * performance-ipc for the /performance hotkey remount test (#12286): the real
 * module, except that the UI command boundary records each command instead of
 * sending it, so one Space press can be counted as exactly one dispatch.
 * Imported by relative path so the test's alias does not loop back here.
 */
export * from '../../../src/lib/rb/performance-ipc.svelte';

export type StubCommand = { type: string; [key: string]: unknown };

export const sent: StubCommand[] = [];

/** Record the command the listener sent and answer ok, as the real bus does. */
export async function runPerformanceCommandFromUi(command: StubCommand): Promise<{ ok: true }> {
	sent.push({ ...command });
	return { ok: true };
}
