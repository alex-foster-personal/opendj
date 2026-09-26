/**
 * On-demand loaders for the MIDI drawer and its learn-log pop-out.
 *
 * Both render only while their midiUi flag is set, so TopBar mounts them
 * inside `{#if}` + `{#await}` rather than importing them statically: the
 * drawer's device list, learn log and takeover settings are not /performance
 * first-paint weight on a boot that never opens them. Same shape as
 * hotkeys/install-hotkeys-overlay.ts. A failed load is not swallowed: TopBar's
 * `{:catch}` renders it as an alert where the surface would have opened, and
 * it is logged here.
 *
 * Requirements (mini-PRD):
 *   ✔︎ a failed chunk fetch is logged and forgotten, so the next open imports
 *     again instead of awaiting a stale rejection.
 *     [if] a second open after a failure reuses the rejected promise [then ⛔️] broken
 *     [if] a failed load leaves no console error [then ⛔️] broken
 */

/** One cached import per surface; a rejected attempt is dropped so the next
 * call fetches again. */
function _onDemand<T>(surface: string, load: () => Promise<T>): () => Promise<T> {
	let pending: Promise<T> | null = null;
	return () => {
		if (pending === null) {
			const attempt = load();
			pending = attempt;
			attempt.catch((error: unknown) => {
				if (pending === attempt) pending = null;
				console.error(`[midi-panel] ${surface} failed to load`, error);
			});
		}
		return pending;
	};
}

export const loadMidiPanel = _onDemand('MIDI panel', () => import('../MidiPanel.svelte'));

export const loadMidiLearnLogPopout = _onDemand(
	'MIDI learn-log pop-out',
	() => import('./MidiLearnLogPopout.svelte')
);

/** The alert line TopBar renders in place of a surface whose chunk did not arrive. */
export function midiSurfaceLoadFailure(surface: string, error: unknown): string {
	const message = error instanceof Error ? error.message : String(error);
	return `${surface} failed to load: ${message}. Reload the page to retry.`;
}
