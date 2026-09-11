import { pinStatus, type LifecyclePin, type PinStatus } from "./feedback";

/**
 * The reply convention that marks a pin PARTIALLY fixed (pin 58a16ac781db,
 * follow-on to #907), without a new persisted status value.
 *
 * Split out of feedback.ts (Fri 5 Sep 2026) to keep that file under the
 * 600-line file-size gate, the same way feedback-pin-seen.ts and
 * feedback-note.ts already split storage-boundary code out of
 * FeedbackWidget.svelte - see Amendment 17 in this batch's packet rules:
 * extraction, never a raised budget.
 *
 * `PinStatus` is only ever open/issued/fixed/merged/archived - there is no
 * "some of this is done, some is not" state, and adding one for real would
 * mean the daemon's PATCH validator (`feedback_pins.py` `_PATCHABLE_STATUSES`)
 * accepting a new enum member: a daemon-side schema change, not a frontend
 * one, and out of scope here (see this pin's PR body for the sizing call).
 *
 * `agent_note` is already a persisted, freely-authored field (#873: "the
 * one-paragraph reply the widget renders under the original text"), so
 * "partial" is derived entirely from ITS TEXT via a convention: a reply that
 * leads with the literal prefix `PARTIAL:` (case-insensitive) AND names
 * where the remaining work went - a `#123` issue/PR reference, or an
 * `http(s)` URL - anywhere in the note. Requiring the reference is
 * deliberate: a bare "PARTIAL:" says a defect is unfinished without saying
 * where the rest of it went, which is exactly the ambiguity pin 58a16ac781db
 * asked to close.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 isPartialNote/pinVisualState: a HALF-FIXED pin is a reply
 *     convention, not a new persisted status - a `PARTIAL:`-prefixed
 *     agent_note naming where the remainder went (a `#123` ref or a URL)
 *     paints an open/issued pin's marker half-fixed; a fixed/merged/archived
 *     pin is never downgraded by one.
 *     [if] `PARTIAL:` alone with no remaining-work reference reads as
 *       partial [then ⛔️] broken - the maintainer would never see where the rest went
 *     [if] a fixed pin regresses to partial because its note happens to
 *       start with PARTIAL: [then ⛔️] broken
 */
const PARTIAL_NOTE_PREFIX = /^partial:/i;
const REMAINING_WORK_REF = /(#\d+|https?:\/\/\S+)/;

export function isPartialNote(note: string | null | undefined): boolean {
  if (!note) return false;
  const trimmed = note.trim();
  return PARTIAL_NOTE_PREFIX.test(trimmed) && REMAINING_WORK_REF.test(trimmed);
}

/** The four states a pin's MARKER can render as: every `PinStatus`, plus the
 * derived `partial` overlay. Never persisted - always recomputed from
 * `status` and `agent_note`. */
export type PinVisualState = PinStatus | "partial";

/**
 * What FeedbackPinMarkers should draw. Identical to `pinStatus()` except an
 * open/issued pin whose `agent_note` follows the PARTIAL: convention paints
 * as the fourth, half-fixed state instead.
 *
 * fixed/merged/archived are never downgraded to partial: those are already
 * resolved (or gone) and a stray "PARTIAL:"-shaped note on one of them must
 * not regress an already-done pin's marker.
 */
export function pinVisualState(pin: LifecyclePin): PinVisualState {
  const status = pinStatus(pin);
  if ((status === "open" || status === "issued") && isPartialNote(pin.agent_note)) {
    return "partial";
  }
  return status;
}
