import { PIN_DRAFT_KEY, serializePinDraft, type PinDraft } from "./feedback";

/**
 * Park (or clear) the unsent comment-pin draft, reporting rather than
 * swallowing a storage failure (Sol review r3941617668, FeedbackWidget.svelte
 * :164). A private/incognito window, storage disabled by policy, or a full
 * quota all throw here, and silently continuing left the exact data-loss
 * condition REFRESH-01 and the draft-persistence feature exist to prevent
 * completely invisible. `onError` is the caller's single hook to surface
 * that - this module never imports a toast/report channel itself, so it
 * stays plain and unit-testable with a fake `Storage` whose `setItem`
 * throws. The in-memory `pinDraft` is never touched here: only the on-disk
 * copy can fail, so a write failure leaves the user's typed text exactly
 * where it was.
 */
export function persistParkedPinDraft(
  storage: Storage,
  pinDraft: PinDraft | null,
  foreignDraftParked: boolean,
  onError: (err: unknown) => void,
): void {
  try {
    if (pinDraft === null || pinDraft.text.trim() === "") {
      // A foreign-page draft was deliberately left out of local state, not
      // cleared by the user - do not let its absence here delete the copy
      // still parked in storage for its own page.
      if (!foreignDraftParked) storage.removeItem(PIN_DRAFT_KEY);
    } else {
      storage.setItem(PIN_DRAFT_KEY, serializePinDraft(pinDraft));
    }
  } catch (err) {
    onError(err);
  }
}
