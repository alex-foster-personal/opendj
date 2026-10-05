/**
 * MIDI browse-step and LOAD selection. Pulled out of BrowserPanel so that
 * component stays within the file-size ratchet. A context menu owns arrow
 * and Enter while it is open (IOPIN-01), so both helpers no-op then.
 */
import type { BrowserRow } from './pane-contract.svelte';

function contextMenuOpen(): boolean {
	return typeof document !== 'undefined' && document.querySelector('[data-testid="context-menu"]') !== null;
}

export function nextMidiSelection(
	visibleRows: readonly BrowserRow[],
	selectedId: string | null,
	delta: number
): BrowserRow | null {
	if (contextMenuOpen() || visibleRows.length === 0 || delta === 0) return null;
	const current = selectedId === null ? -1 : visibleRows.findIndex((row) => row.stable_id === selectedId);
	const next =
		current === -1
			? delta > 0
				? 0
				: visibleRows.length - 1
			: Math.max(0, Math.min(visibleRows.length - 1, current + delta));
	return visibleRows[next] ?? null;
}

/** undefined: menu is open. null: nothing selected. */
export function midiLoadRow(
	visibleRows: readonly BrowserRow[],
	selectedId: string | null
): BrowserRow | null | undefined {
	if (contextMenuOpen()) return undefined;
	if (selectedId === null) return null;
	return visibleRows.find((candidate) => candidate.stable_id === selectedId) ?? null;
}
