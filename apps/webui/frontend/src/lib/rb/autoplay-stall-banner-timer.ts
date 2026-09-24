/**
 * Pure timer helpers for the transient AutoPlay stall banner (issue #3882).
 */

export const AUTOPLAY_STALL_BANNER_DISMISS_MS = 5000;
export const AUTOPLAY_STALL_BANNER_HOVER_MS = 10000;

/** Milliseconds until the banner should hide for the current stall revision. */
export function autoplayStallBannerDismissMs(hovered: boolean): number {
	return hovered ? AUTOPLAY_STALL_BANNER_HOVER_MS : AUTOPLAY_STALL_BANNER_DISMISS_MS;
}

/** Whether a stall revision should show the on-screen banner again. */
export function shouldShowAutoplayStallBanner(
	stallRevision: number | null,
	hiddenForRevision: number | null
): boolean {
	if (stallRevision === null) return false;
	return hiddenForRevision !== stallRevision;
}
