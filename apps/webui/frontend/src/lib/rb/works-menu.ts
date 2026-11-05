/** Context-menu entry points for works: open the right modal for a selection. */
import { linkTracks, reportWorksFailure, worksView } from './works-overlay.svelte';

export function openWorkEditor(stableIds: string[], title: string): void {
	worksView.modal = { kind: 'editor', stableIds, title };
}

/** One track picks a version from the library; two or more stack straight away. */
export function linkVersionsFromMenu(stableIds: string[], title: string): void {
	if (stableIds.length === 1) {
		worksView.modal = { kind: 'link', stableId: stableIds[0] };
		return;
	}
	linkTracks(stableIds, { title }).catch((exc: unknown) => {
		reportWorksFailure('link failed', exc);
	});
}
