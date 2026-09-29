/** Map measured hot-cue row height to how many lyric lines the deck may show. */
export function deckLyricRowBudget(cueFlexHeightPx: number): 1 | 2 | 3 {
	if (!Number.isFinite(cueFlexHeightPx) || cueFlexHeightPx <= 0) return 1;
	if (cueFlexHeightPx >= 72) return 3;
	if (cueFlexHeightPx >= 48) return 2;
	return 1;
}
