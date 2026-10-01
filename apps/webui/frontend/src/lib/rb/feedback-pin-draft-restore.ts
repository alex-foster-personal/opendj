import { parsePinDraft, PIN_DRAFT_KEY, type PinDraft } from "./feedback";
import { parsePinPlacement, type PinPlacement } from "./feedback-pin-position";

/** The parked draft's placement record, read separately from parsePinDraft
 * so that file stays under its size gate. A half-shaped placement drops only
 * the placement (the pin then saves viewport-only, as every pin used to);
 * losing the typed text over it would be the worse failure. */
function _restorePlacement(raw: string | null): PinPlacement | null {
  if (raw === null) return null;
  try {
    return parsePinPlacement((JSON.parse(raw) as Record<string, unknown>).placement);
  } catch {
    return null;
  }
}
/**
 * Restore a parked draft on mount, or report it as belonging to another
 * page. A draft's point/anchor are only meaningful on the page they were
 * placed on (r3919185341), so one parked on a different page is stale here,
 * not restorable - reported back as `foreign: true` rather than reattached
 * to whatever page happens to remount the widget, so the caller can leave
 * its own storage entry alone instead of deleting a draft that still
 * belongs to its own page. The follow-on parent link and viewport now
 * round-trip through parsePinDraft, so a refresh mid-follow-on keeps the
 * reply attached to its parent instead of silently downgrading to a plain
 * top-level draft; `currentViewport` only backfills a draft saved before
 * that field existed.
 */
export function restoreParkedPinDraft(
  raw: string | null,
  pathname: string,
  currentViewport: { width: number; height: number },
): {
  draft: (PinDraft & { viewport: { width: number; height: number }; followOn: { parentId: string; label: string } | null; placement: PinPlacement | null }) | null;
  foreign: boolean;
} {
  const restored = parsePinDraft(raw);
  const foreign = restored !== null && restored.page !== pathname;
  const draft =
    foreign || restored === null
      ? null
      : {
          ...restored,
          viewport: restored.viewport ?? currentViewport,
          followOn: restored.followOn ?? null,
          placement: _restorePlacement(raw),
        };
  return { draft, foreign };
}

/**
 * `restoreParkedPinDraft` above, but reading `raw` from a real `Storage`
 * itself rather than taking it as an argument - the seam that needs a try/
 * catch, since `storage.getItem` throws in a private/incognito window or
 * when storage is disabled by policy (Sol review r3941617668,
 * FeedbackWidget.svelte:164). `restoreParkedPinDraft`/`parsePinDraft` never
 * throw - a corrupt stored VALUE already reads as "no draft" by design,
 * covered by the "reads garbage as no draft" case - so the only failure
 * this catches is storage access itself being unavailable, and `onError`
 * is the caller's one hook to report that rather than let it collapse
 * silently into the same "no draft" outcome a merely-corrupt value gets.
 */
export function readParkedPinDraft(
  storage: Storage,
  pathname: string,
  currentViewport: { width: number; height: number },
  onError: (err: unknown) => void,
): ReturnType<typeof restoreParkedPinDraft> {
  try {
    return restoreParkedPinDraft(storage.getItem(PIN_DRAFT_KEY), pathname, currentViewport);
  } catch (err) {
    onError(err);
    return { draft: null, foreign: false };
  }
}
