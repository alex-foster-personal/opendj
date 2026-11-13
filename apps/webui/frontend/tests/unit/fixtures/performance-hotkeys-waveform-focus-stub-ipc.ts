/**
 * Stand-in for `$lib/rb/performance-ipc.svelte` in the waveform-focus hotkey
 * tests (#12274): the real module, except that runPerformanceCommandFromUi
 * records each command instead of reaching the engine. A local export wins
 * over the star re-export, so every other import still gets the real code.
 */
import type { PerformanceCommand } from '../../../src/lib/rb/performance-ipc.svelte';

export * from '../../../src/lib/rb/performance-ipc.svelte';

export const sent: PerformanceCommand[] = [];

/** Records the command the listener sent, then answers ok. */
export async function runPerformanceCommandFromUi(
	command: PerformanceCommand
): Promise<{ ok: true }> {
	sent.push(command);
	return { ok: true };
}
