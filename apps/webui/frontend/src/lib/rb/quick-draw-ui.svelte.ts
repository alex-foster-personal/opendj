/** Shared UI state for the performance quick-draw menu (not engine state). */
export const quickDrawUi = $state({
	menuHighlightStableId: null as string | null
});

export function setMenuHighlightStableId(id: string | null): void {
	quickDrawUi.menuHighlightStableId = id;
}
