import type { ContextMenuItem } from '../ContextMenu.svelte';
import type { BrowserRow } from './pane-contract.svelte';

export type TrackMenuCallbacks = {
	onstemsdonext?: ((stableIds: string[]) => void) | undefined;
	onlyricsdonext?: ((stableIds: string[]) => void) | undefined;
};

/** Selection rule: multi-select if the row is selected, else just this row. */
export function idsForTrackAction(row: BrowserRow, selectedIds: string[]): string[] {
	return selectedIds.includes(row.stable_id) ? selectedIds : [row.stable_id];
}

export function stemsAndLyricsMenuItems(
	row: BrowserRow,
	selectedIds: string[],
	cb: TrackMenuCallbacks
): ContextMenuItem[] {
	const selected = idsForTrackAction(row, selectedIds);
	return [
		{
			id: 'stems-generate',
			label: 'Stems: do next',
			run: cb.onstemsdonext ? () => cb.onstemsdonext?.(selected) : undefined
		},
		{
			id: 'lyrics',
			label: 'Lyrics: do next',
			run: cb.onlyricsdonext ? () => cb.onlyricsdonext?.(selected) : undefined
		}
	];
}
